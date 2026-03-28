# Business Customization Guide

这个目录属于业务可编辑区。

新业务从 scaffold 初始化后，推荐按下面顺序替换 demo：

1. 修改仓库名、README 标题和包名
2. 在 `src/.../config.py` 中替换默认业务配置
3. 用真实业务模型替换 `models/`
4. 用真实业务 mapper / validator 替换 `mappers/` 与 `validators/`
5. 在 `workflows/` 中定义自己的 step 划分
6. 在 `tasks/__init__.py` 中替换默认 task 列表
7. 用真实 smoke case 替换 `tests/test_workflow_demo.py`

不要做的事：

- 不要直接改 vendored contract docs
- 不要把 `workflow_draft.review-only.yaml` 当成可执行 workflow
- 不要从 framework 内部模块拷实现到业务仓库

