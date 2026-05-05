import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const rendererPackageJson =
  process.env.RENDERER_PACKAGE_JSON || import.meta.url;
const require = createRequire(rendererPackageJson);
const echarts = require("echarts");
const sharp = require("sharp");

const [, , inputPath, outputDir] = process.argv;
if (!inputPath || !outputDir) {
  throw new Error("Usage: node fastmoss_visualization_renderer.mjs <input.json> <output_dir>");
}

const input = JSON.parse(fs.readFileSync(inputPath, "utf8"));
const productId = String(input.product_id || "").trim();
const overview = input.overview || {};
const sku = input.productSku || input.product_sku || {};
const charts = Array.isArray(input.charts)
  ? input.charts
  : ["marketing_strategy", "overview_trend", "sku_analysis"];

fs.mkdirSync(outputDir, { recursive: true });

const W = 2048;
const FONT = "-apple-system, BlinkMacSystemFont, 'PingFang SC', 'Microsoft YaHei', Arial, sans-serif";
const INK = "#1F283C";
const MUTED = "#667D98";
const VI = "#FE2062";
const CARD = "#FAFAFA";
const CARD_ALT = "#FFF2F3";
const SELECT_BORDER = "#FFC1C1";
const WHITE = "#FFFFFF";
const BLUE_BAR = "#ECF1FF";
const PIE_COLORS = ["#FF6593", "#91EFC7", "#688FFF"];
const LINE_COLORS = ["#fe2062", "#43d7c8", "#5a6fc0", "#9ecb7f"];

const renderers = {
  marketing_strategy: renderMarketingStrategy,
  overview_trend: renderOverviewTrend,
  sku_analysis: renderSkuAnalysis,
};

const outputs = {};
for (const chartName of charts) {
  if (!renderers[chartName]) {
    throw new Error(`Unsupported FastMoss visualization chart: ${chartName}`);
  }
  outputs[chartName] = await renderers[chartName]();
}

console.log(
  JSON.stringify({
    product_id: productId,
    output_dir: outputDir,
    outputs,
  }),
);

async function renderMarketingStrategy() {
  const gap = 16;
  const margin = 24;
  const cardW = (W - margin * 2 - gap) / 2;
  const cardH = 258;
  const y1 = 74;
  const y2 = y1 + cardH + gap;
  const parts = [
    svgOpen(W, 630),
    text("营销策略", margin, 38, 22, 700),
    distributionCard({
      x: margin,
      y: y1,
      w: cardW,
      h: cardH,
      title: "成交渠道占比",
      data: overview.channel_distribution?.units_sold,
      type: "sold_count",
      distributionType: "source",
    }),
    distributionCard({
      x: margin + cardW + gap,
      y: y1,
      w: cardW,
      h: cardH,
      title: "成交内容占比",
      data: overview.content_distribution?.units_sold,
      type: "sold_count",
      distributionType: "category",
    }),
    distributionCard({
      x: margin,
      y: y2,
      w: W - margin * 2,
      h: cardH,
      title: "成交投放占比",
      data: overview.ads_distribution?.units_sold,
      type: "sold_count",
      distributionType: "category",
    }),
    svgClose(),
  ];
  return writePng("marketing_strategy.png", parts.join(""));
}

async function renderOverviewTrend() {
  const margin = 24;
  const cardGap = 8;
  const cardW = (W - margin * 2 - cardGap * 3) / 4;
  const cardH = 123;
  const cardsY = 64;
  const chartY = cardsY + cardH * 2 + cardGap + 24;
  const chartH = 427;
  const metricCards = overviewMetricCards(overview.overview || {});
  const parts = [svgOpen(W, chartY + chartH + 24), text("概览", margin, 38, 22, 700)];

  metricCards.forEach((card, index) => {
    const selected = index < 2;
    const x = margin + (index % 4) * (cardW + cardGap);
    const y = cardsY + Math.floor(index / 4) * (cardH + cardGap);
    parts.push(
      rect(x, y, cardW, cardH, 12, selected ? CARD_ALT : CARD, selected ? SELECT_BORDER : CARD, 1),
      selected ? checkedIcon(x + cardW - 34, y + 13) : emptyCheck(x + cardW - 34, y + 13),
      text(card.value, x + 16, y + 34, 18, 700),
      card.desc ? text(`日均${card.desc}`, x + 16, y + 62, 13, 500, MUTED) : "",
      text(card.label, x + 16, y + cardH - 20, 14, 500, MUTED),
    );
  });

  parts.push(
    rect(margin, chartY, W - margin * 2, chartH, 16, CARD),
    text("销量 / 销售额", margin + 16, chartY + 38, 16, 600),
    segmentedControl(margin + W - 24 - 302, chartY + 20, ["增量", "总量"], 0),
    pinkButton("查看表格", margin + W - 24 - 120, chartY + 20, 96, 32),
  );

  const chartSvg = renderEchartsSvg(
    W - margin * 2 - 80,
    353,
    fastmossOverviewLineOption(overview.chart_list || []),
  );
  parts.push(imageSvg(chartSvg, margin + 40, chartY + 74, W - margin * 2 - 80, 353));
  parts.push(svgClose());
  return writePng("overview_trend.png", parts.join(""));
}

async function renderSkuAnalysis() {
  const margin = 24;
  const gap = 16;
  const cardW = (W - margin * 2 - gap) / 2;
  const cardH = 258;
  const yCards = 214;
  const propKey = Object.keys(sku.sku_units_sold || {})[0] || "";
  const filterLabel = propKey || "Color";
  const filterBadgeW = Math.max(84, Math.min(180, estimatedTextWidth(filterLabel, 14) + 34));
  const best = sku.best_sku || {};
  const parts = [
    svgOpen(W, yCards + cardH + 24),
    text("SKU分析", margin, 38, 22, 700),
    text("近7天", W - 110, 32, 14, 600, MUTED),
    text("近28天", W - 54, 32, 14, 700, VI),
    rect(margin, 64, W - margin * 2, 44, 8, BLUE_BAR),
    text(`表现 最优SKU：${best.sku_name || propKey} - ${best.sku_value || ""}`, margin + 44, 92, 14, 700),
    text("|", margin + 360, 92, 14, 500, MUTED),
    text(`库存：${best.stock ?? "-"}`, margin + 382, 92, 14, 500),
    text("|", margin + 460, 92, 14, 500, MUTED),
    text(`价格：${best.price ?? "-"}`, margin + 482, 92, 14, 500),
    text("筛选规则：", margin, 158, 14, 500, MUTED),
    rect(margin + 82, 136, filterBadgeW, 34, 8, WHITE, VI, 1),
    centeredText(filterLabel, margin + 82 + filterBadgeW / 2, 158, 14, 600, VI),
    distributionCard({
      x: margin,
      y: yCards,
      w: cardW,
      h: cardH,
      title: "成交内容占比(近28天)",
      data: sku.sku_units_sold?.[propKey],
      type: "sold_count",
      distributionType: "source",
      hideValueColumn: true,
    }),
    distributionCard({
      x: margin + cardW + gap,
      y: yCards,
      w: cardW,
      h: cardH,
      title: "库存占比（当前）",
      data: sku.sku_stock?.[propKey],
      type: "sold_count",
      distributionType: "source",
    }),
    svgClose(),
  ];
  return writePng("sku_analysis.png", parts.join(""));
}

function distributionCard({
  x,
  y,
  w,
  h,
  title,
  data,
  type,
  distributionType,
  hideValueColumn = false,
}) {
  if (!data) return "";
  const bodyY = y + 82;
  const chartX = x + 32;
  const chartY = bodyY;
  const tableX = chartX + 153 + 24;
  const tableW = w - (tableX - x) - 16;
  const tableH = 153;
  return [
    rect(x, y, w, h, 16, CARD),
    text(title, x + 16, y + 38, 16, 600),
    title.includes("库存") ? "" : segmentedControl(x + w - 180, y + 18, ["总销量", "总销售额"], 0),
    imageSvg(renderDistributionPieSvg(data, type, distributionType), chartX, chartY, 153, 153),
    distributionTable(data, type, distributionType, tableX, bodyY, tableW, tableH, hideValueColumn),
  ].join("");
}

function distributionTable(data, type, distributionType, x, y, w, h, hideValueColumn) {
  const list = data.list || [];
  const header = data.header || [];
  const rowTop = y + 54;
  const rowGap = list.length > 3 ? 28 : 34;
  const col1W = hideValueColumn ? w * 0.72 : w * 0.58;
  const col2X = x + col1W;
  const col3X = x + col1W + w * 0.2;
  const parts = [rect(x, y, w, h, 16, WHITE)];
  parts.push(text(translateHeader(header[0]), x + 18, y + 33, 14, 700, MUTED));
  parts.push(text("占比", col2X, y + 33, 14, 700, MUTED));
  if (!hideValueColumn) {
    parts.push(text(translateHeader(header[2]), col3X, y + 33, 14, 700, MUTED));
  }
  list.forEach((row, index) => {
    const color = PIE_COLORS[index % PIE_COLORS.length];
    const rowY = rowTop + index * rowGap;
    const label = translate(row[distributionType]);
    parts.push(circle(x + 24, rowY - 4, 6, color));
    parts.push(text(label, x + 38, rowY, 14, 400));
    parts.push(text(row.propotion ?? "", col2X, rowY, 14, 400));
    if (!hideValueColumn) {
      parts.push(text(valueShow(row, type), col3X, rowY, 14, 400));
    }
  });
  return parts.join("");
}

function renderDistributionPieSvg(data, type, distributionType) {
  const list = data.list || [];
  return renderEchartsSvg(153, 153, {
    color: PIE_COLORS,
    series: [
      {
        name: "Access From",
        type: "pie",
        radius: ["60%", "90%"],
        avoidLabelOverlap: false,
        label: {
          position: "center",
          width: 130,
          overflow: "truncate",
          ellipsis: "...",
          show: false,
        },
        emphasis: { label: { show: true, fontSize: 12, fontWeight: "bold" } },
        labelLine: { show: false },
        data: list.map((row) => ({
          name: translate(row[distributionType]),
          value: Number(row[type] || 0),
        })),
      },
    ],
  });
}

function fastmossOverviewLineOption(rows) {
  const seriesDefs = [
    {
      name: "销量",
      data: rows.map((row) => Number(row.inc_sold_count || 0)),
      dataShow: rows.map((row) => row.inc_sold_count_show ?? row.inc_sold_count ?? 0),
    },
    {
      name: "销售额",
      data: rows.map((row) => Number(row.inc_sale_amount || 0)),
      dataShow: rows.map((row) => row.inc_sale_amount_show ?? row.inc_sale_amount ?? 0),
    },
  ];
  return {
    color: LINE_COLORS,
    grid: { left: "4%", right: "4%", bottom: "10%", top: "5%", containLabel: false },
    xAxis: [
      {
        type: "category",
        data: rows.map((row) => row.dt),
        axisLabel: {
          hideOverlap: true,
          show: true,
          color: MUTED,
          formatter: (value) => String(value ?? "").replace(" ", "\n"),
        },
        axisPointer: {
          type: "line",
          snap: true,
          label: { show: true, formatter: (value) => value.value },
        },
      },
    ],
    yAxis: seriesDefs.map((serie, index) => {
      const maxValue = Math.max(...serie.data, 0);
      const rounded = exactFastmossAxisRound(maxValue);
      const interval = Math.ceil(Math.ceil(rounded) / 5);
      return {
        type: "value",
        min: 0,
        logBase: 3,
        splitNumber: 5,
        alignTicks: true,
        selected: true,
        position: ["left", "right"][index],
        interval,
        max: 5 * interval,
        axisLabel: { formatter: formatCompact },
      };
    }),
    series: seriesDefs.map((serie, index) => ({
      animation: false,
      showSymbol: false,
      name: serie.name,
      type: "line",
      lineStyle: { color: LINE_COLORS[index] },
      yAxisIndex: [0, 1][index],
      smooth: false,
      data: serie.data,
      selected: true,
    })),
    tooltip: {
      trigger: "axis",
      backgroundColor: MUTED,
      borderColor: MUTED,
      textStyle: { color: WHITE },
      extraCssText: "border-radius: 8px;",
    },
  };
}

function overviewMetricCards(data) {
  return [
    { value: data.sold_count_show, desc: data.avg_sold_count_show, label: "销量" },
    { value: data.sale_amount_show, desc: data.avg_sale_amount_show, label: "销售额" },
    { value: data.author_count_show, label: "带货达人数量" },
    { value: data.aweme_count_show, label: "带货视频数" },
    { value: data.live_count_show, label: "带货直播数" },
    { value: data.price, label: "成交均价" },
    { value: data.video_sale_amount_show, label: "视频销售额" },
    { value: data.live_sale_amount_show, label: "直播销售额" },
  ];
}

function renderEchartsSvg(width, height, option) {
  const chart = echarts.init(null, null, { renderer: "svg", ssr: true, width, height });
  chart.setOption(
    {
      animation: false,
      backgroundColor: "transparent",
      textStyle: { fontFamily: FONT, color: INK },
      ...option,
    },
    true,
  );
  const svg = chart.renderToSVGString();
  chart.dispose();
  return svg;
}

async function writePng(fileName, svg) {
  const out = path.join(outputDir, fileName);
  await sharp(Buffer.from(svg)).png({ compressionLevel: 9 }).toFile(out);
  return out;
}

function svgOpen(width, height) {
  return `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}"><rect width="${width}" height="${height}" fill="#fff"/>`;
}

function svgClose() {
  return "</svg>";
}

function imageSvg(svg, x, y, w, h) {
  const encoded = Buffer.from(svg).toString("base64");
  return `<image x="${x}" y="${y}" width="${w}" height="${h}" href="data:image/svg+xml;base64,${encoded}"/>`;
}

function rect(x, y, w, h, r, fill, stroke = "", strokeWidth = 0) {
  const strokeAttrs = stroke ? ` stroke="${stroke}" stroke-width="${strokeWidth}"` : "";
  return `<rect x="${x}" y="${y}" width="${w}" height="${h}" rx="${r}" ry="${r}" fill="${fill}"${strokeAttrs}/>`;
}

function circle(cx, cy, r, fill) {
  return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="${fill}"/>`;
}

function text(value, x, y, size, weight = 400, fill = INK) {
  return `<text x="${x}" y="${y}" fill="${fill}" font-size="${size}" font-weight="${weight}" font-family="${escapeXml(FONT)}">${escapeXml(value ?? "")}</text>`;
}

function centeredText(value, x, y, size, weight = 400, fill = INK) {
  return `<text x="${x}" y="${y}" text-anchor="middle" fill="${fill}" font-size="${size}" font-weight="${weight}" font-family="${escapeXml(FONT)}">${escapeXml(value ?? "")}</text>`;
}

function estimatedTextWidth(value, size) {
  return String(value ?? "").length * size * 0.62;
}

function segmentedControl(x, y, labels, selectedIndex) {
  const itemW = 82;
  const h = 30;
  const parts = [rect(x, y, itemW * labels.length, h, 8, "#F5F5F5")];
  labels.forEach((label, index) => {
    if (index === selectedIndex) {
      parts.push(rect(x + index * itemW, y, itemW, h, 8, CARD_ALT));
    }
    parts.push(
      `<text x="${x + index * itemW + itemW / 2}" y="${y + 20}" text-anchor="middle" fill="${index === selectedIndex ? VI : MUTED}" font-size="13" font-weight="700" font-family="${escapeXml(FONT)}">${escapeXml(label)}</text>`,
    );
  });
  return parts.join("");
}

function pinkButton(label, x, y, w, h) {
  return [
    rect(x, y, w, h, 8, CARD_ALT),
    `<text x="${x + w / 2}" y="${y + 21}" text-anchor="middle" fill="${VI}" font-size="13" font-weight="700" font-family="${escapeXml(FONT)}">${escapeXml(label)}</text>`,
  ].join("");
}

function checkedIcon(x, y) {
  return [
    circle(x + 9, y + 9, 9, VI),
    `<path d="M${x + 5} ${y + 9.5} L${x + 8} ${y + 12.5} L${x + 14} ${y + 6}" fill="none" stroke="#fff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>`,
  ].join("");
}

function emptyCheck(x, y) {
  return `<rect x="${x}" y="${y}" width="18" height="18" rx="5" fill="#fff" stroke="#DADDE3" stroke-width="2"/>`;
}

function exactFastmossAxisRound(value) {
  let current = Number(value || 0);
  let power = 0;
  if (current < 10) return 10;
  while (current >= 10) {
    current /= 10;
    power += 1;
  }
  return Math.ceil(current) * 10 ** power;
}

function valueShow(row, type) {
  const showKey = `${type}_show`;
  if (row[showKey] !== undefined) return row[showKey];
  return row[type] ?? "";
}

function translateHeader(key) {
  return (
    {
      "common.goods.source": "类目",
      "product.category": "类目",
      "common.goods.sku": "规格",
      "common.goods.propotion": "占比",
      "common.orders": "销量",
      "common.goods.GMV": "销售额",
      "common.goods.stock": "库存",
    }[key] || translate(key)
  );
}

function translate(key) {
  return (
    {
      "common.goods.product_card": "商品卡",
      "common.goods.shop_account": "店铺自营号",
      "common.goods.affiliate": "达人带货",
      "video.name": "视频",
      "live.name": "直播",
      "common.goods.adTraffic": "广告投放（视频）",
      "common.goods.otherTraffic": "其他流量",
      Other: "Other",
    }[key] || String(key ?? "")
  );
}

function formatCompact(value) {
  const number = Number(value || 0);
  if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(0)}万`;
  if (Math.abs(number) >= 1000) return `${(number / 1000).toFixed(0)}K`;
  return String(Math.round(number));
}

function escapeXml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}
