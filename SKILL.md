---
name: anima-style-lora-caption
description: 指导 agent 为 Anima LoRA 数据集执行打标/洗标：探测环境与代理、下采样、生成 tag 动作表与缺口矩阵、视觉前角色名录消歧、派发看图子代理输出严格 JSON、解析合并、两道校验并原子写回。产出单行空格形 danbooru 标签串与 caption_summary.json。用户要求"洗标/打标/给数据集写 caption/整理标签/清洗 tag"时使用。
---

# SKILL：Anima LoRA 数据集打标/洗标执行器

本 skill 编排端到端打标/洗标执行；规则 SSOT 在 `rules/`，执行细则在 `assets/`，标签内容规范按需查阅 `references/`。

> **不预设本地路径、工具名、硬件或网络环境。** 第 0 步先探测确认再开工。

## §0 环境探测（动手前必须完成）

| 项 | 探测内容 | 影响 |
|---|---|---|
| **数据集与非图文件** | 目录划分、图片数、体积（>8MB 需下采样）、是否混有 `.zip`/`.mp4`/`.psd` | 预清理与分批 |
| **初稿与权威源** | 同名 `.txt`（散文/标签）及 metadata/rename_map/CSV 等 booru 权威源 | 三源（A>B>C）/两源（B/C）/纯视觉（B） |
| **工具名与子代理模式** | 探测 `<READ_IMAGE_TOOL>`（如 `read_image`/`Read`）、`<WRITE_TOOL>`（如 `write_file`/`Write`），及是否支持 teammate 复用/轮转（单代理上限 5~8 张） | 提示词占位替换与轮转策略 |
| **网络与代理探针** | 探测 `danbooru.donmai.us`、`animadex.net` 连通性；代理 `--proxy` 及 `urllib` vs `curl -x`（`--use-curl`）可用性 | Stage 1.5b 与 Gate 2 在线校验 |
| **Python / 输出 / 触发词** | Python ≥3.10 + `Pillow`；`<WORK>` 与 `<BACKUP_DIR>` 路径；首位 `@trigger`（禁止从文件名反推） | 脚本执行与落盘锚点 |

探测结果用一句话向用户复述确认后再开工。

## §1 资产与脚本索引（按需渐进加载）

> **直接运行 `scripts/*.py` CLI**，严禁每次从零手写下采样/解析/合并/校验样板脚本。

| 路径 | 职责与加载时机 |
|---|---|
| `rules/` | **SSOT 规则**（`vision_fix.json`、`whitelist.txt`、`parent_fold.json`、`banned.json`、`neg_keep.txt`、`slots.json`、`prompt_templates.json`）——脚本自动加载，勿手抄副本 |
| `scripts/` | **CLI 工具链**（`common.py`、`make_thumbs.py`、`build_tag_table.py`、`lookup_chars.py`、`parse_descriptions.py`、`merge_captions.py`、`validate_captions.py`、`apply_captions.py`） |
| [assets/workflow.md](assets/workflow.md) | **执行详规**：提示词模板（严格 JSON）、缺口预设、轮转纪律、全部脚本 CLI 用法 |
| [assets/normalization.md](assets/normalization.md) | **规范化与校验指南**：`wash_rules.json` 扩展语义、否定式防护、3 类冲突豁免、3 态校验 |
| [references/Anima_style_LoRA_打标指南.md](references/Anima_style_LoRA_打标指南.md) | **核心规范（按需）**：格式硬约束、7 槽位结构、三源优先级、专项条款（写/审标签前先读） |
| [references/target_types.md](references/target_types.md) | **目标类型细则（按需）**：角色/画师/服装/物体/场景差异与 WD14 标定（仅按需读取） |
| [references/troubleshooting.md](references/troubleshooting.md) | **排障手册（按需）**：异常矩阵、风格-内容纠缠、Trainer 源码细节（仅排错时读取） |

## §2 两条铁律

1. **主代理永不读图**：看图一律交子代理，主代理只处理文本、JSON 与脚本，防图片撑爆上下文。
2. **写一张落一张**：子代理每看完一张立刻用 `<WRITE_TOOL>` 写入 `descriptions/<stem>.json`。

## §3 升级版执行流程

```text
阶段 -1   归档初稿 + 轻量预检（隔离非图文件；近重复先查 parent_id/pixiv_id/Jaccard；定水印策略）
阶段  1   下采样 → 缩略图（scripts/make_thumbs.py：长边 ≤1536 / ≤2MB）
阶段 1.5  动作表 + 缺口矩阵 review_todo.csv + char_freq.csv + per_image.json 追加（scripts/build_tag_table.py）
阶段 1.5b 视觉前角色名录消歧（scripts/lookup_chars.py + Pixiv/FANBOX 标题 → 用户签字 → 注入阶段 2）
阶段 2.0  1~2 张缩略图冒烟探针（验证看图通道、审查阈值与严格 JSON 可解析性）
阶段  2   缺口驱动看图派发（动态 SFW/NSFW/任务预设，严格 JSON 落盘，teammate 每 5~8 张轮转重建）
阶段 2.5  解析（scripts/parse_descriptions.py）→ supplement.json + vision_failed.txt + vision_review.txt
阶段  3   合并（scripts/merge_captions.py：三源/两源/纯视觉 + rules/ + overrides.json + 可选 --trim-mode auto）
阶段  4   校验（scripts/validate_captions.py：Gate 1 离线含不含触发词计数与 3 豁免 + Gate 2 在线 3 态 & SQLite 缓存）
阶段  5   快照备份 → 原子写回 → 落盘复检 → caption_summary.json（scripts/apply_captions.py）
```

## §4 按任务形态分流

| 形态 | 特征 | 合并策略 |
|---|---|---|
| **三源（A/B/C）** | 有 booru 权威标签 + 图 + 初稿 | A 为基准不得推翻；B 补空缺；C 仅交叉校验 |
| **两源（B/C）** | 无 booru 源，有自动打标初稿 | B 为骨（视觉即主体）；C 交叉核对，冲突判 C 幻觉 |
| **纯视觉（B）** | 无 booru 源、无初稿 | 视觉产出直接构成 caption；无依据不写 |

## §5 派发子代理要点

- **结构化填表 + 严格 JSON**：槽位清单锚定填表，落盘 `descriptions/<stem>.json`（`string[]` 值 + `"note"` 字符串），避免开放式描述误拒与逗号/颜文字切分损坏。
- **缺口驱动与分级隔离**：按 `review_todo.csv` 选精简/完整预设；仅 NSFW 挂载 `ACTS`/`FLUIDS`；SFW/NSFW 与近重复对隔离。
- **轮转与抗拒答**：DSH teammate 每 **5~8 张轮转重建**；整批拒答缩至 **1 图 1 代理**，仍硬拒换模型/渠道或做四段探针二分。
- **失败只记台账**：空返回/拒答写 `vision_failed.txt`，严禁静默退回文本兜底。

## §6 红线（任何时候不得违反）

1. **不假设环境**：路径、工具名、通道、代理一律先探测确认。
2. **不写自造否定式**（`no cum`/`tongue not out` 零容忍）；合法 `no *` 标签由 `rules/neg_keep.txt` 与词典放行。
3. **不用文件名反推标签**；**不擅自给客串角色定名**（`lookup_chars.py` 名录交用户裁决）。
4. **不自动兜底降级**：失败只记台账。
5. **不将 `UNVERIFIED` 降级为 `INVALID`/`ABSENT`**：网络失败未验证的标签绝不能当作不存在而删掉。
6. **不截断 `per_image.json`**：增量入库只能追加/更新，不得丢失历史批次。
7. **标签数口径一律不含 `@trigger`**：20~45 区间及 `--max-tags` 均排除首位触发词。
8. **不改动原图**；写回前必须快照备份初稿与元数据，并跑完 Gate 1 + Gate 2（未验证项未显式放行前阻断写回）。

## §7 完成后交付

- 产出路径（`<WORK>`、快照备份目录、`caption_summary.json`）
- 处理统计（总张数、`vision_failed.txt`、不含 `@trigger` 的标签数均值/中位/极值及裁剪明细）
- 校验报告（Gate 1 问题数、Gate 2 `VALID`/`INVALID`/`UNVERIFIED` 分布、`vision_review.txt`）
- 数据集问题（近重复对、多样性缺口、待裁决事项）
- **训练交接**：转 `anima-lora-config` skill，传入 `apply_captions.py` 产出的 `caption_summary.json`。
