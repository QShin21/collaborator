# 存档：伴行 v0.3（草稿路线的桌宠）

这里是 2026-11-17 发布的 v0.3.0 的完整快照（提交 `4dd3ca1`）：代码、测试、方案、证据台账与记录、工作流、随包文件。
**它不再是产品的一部分**，不参与仓库根目录的构建和测试，也不要再改它。现在的应用见仓库根目录的 [`README.md`](../../README.md)
和 [`docs/design.md`](../../docs/design.md)。

v0.3 做的是：待在 Windows 版 Codex 窗口旁边，用户在输入框里写草稿、停一下，伴行给三条路线（一条主线）、或只提醒缺什么、或保持安静；
点一条，把「原话 + 路线」填进输入框。它的 Windows 运行器证据（E2，EV-2026-001 至 011）在 [`docs/`](docs/verification.md) 里，
**只对这里的代码成立**。

| 这里有什么 | |
| --- | --- |
| `banxing/`、`tests/`、`run.py`、`start.bat` | v0.3 的代码与它的 416 项测试 |
| `docs/banxing-generation2.md` | 二代产品方案及其实施状态 |
| `docs/verification.md`、`docs/acceptance.md`、`docs/evidence/` | 证据台账、真机验收手册、十一份证据记录 |
| `docs/product-design.md` | 一代（开工桌宠）的设计 |
| `.github/`、`packaging/`、`CLAUDE.md`、`.env.example`、`requirements-windows.txt` | 当时的工作流、随包文件、项目规则（都只是存档，不会被执行） |

想看它当时的测试还能不能跑：

```
cd legacy/v0.3
python -m unittest discover -s tests
```

`README.v0.3.md` 是当时仓库首页的原文。
