# anima-style-lora-caption

一个 Agent Skill，指导 agent 为 [Anima](https://huggingface.co/circlestone-labs/Anima) LoRA 数据集执行端到端**打标 / 洗标**：探测环境与代理、下采样、生成 tag 动作表与缺口矩阵、视觉前角色名录消歧、派发看图子代理（严格 JSON 落盘）、解析合并、两道校验、原子写回并输出训练配置交接文件 `caption_summary.json`。

**标签内容规范**（格式硬约束、7 槽位结构、三源优先级、专项条款）见 [references/Anima_style_LoRA_打标指南.md](references/Anima_style_LoRA_打标指南.md)。

## 适用场景

- 从零为一批图片生成可用于 Anima LoRA 训练的单行空格形 Danbooru caption
- 清洗/规范化既有初稿（别名、废弃词、自造否定式、下划线/空格混用、模板前导逗号）
- 融合三源（booru 权威标签 / 看图视觉补充 / 自动打标初稿）并保留声明式手工修订（`overrides.json`）

## 目录结构与单一事实源（SSOT）

```text
anima-style-lora-caption/
├── SKILL.md                              # 工作流总纲（环境探测、脚本索引、两条铁律、红线）
├── rules/                                # SSOT 机器可读规则（全脚本共用）
│   ├── vision_fix.json                   # 别名、废弃删除、多词拆分、颜色降级、语义陷阱
│   ├── whitelist.txt                     # 光影/构图/水印颜色描写词白名单
│   ├── parent_fold.json                  # 蕴含父标签折叠 + 修饰词去泛形
│   ├── banned.json                       # meta / 质量词 / 废弃文字族 / score&year 正则
│   ├── neg_keep.txt                      # 合法 Danbooru no-* 标签白名单
│   ├── slots.json                        # 1~7 槽位映射、pose/comp/watermark 词库、裁剪层级、冲突与豁免
│   └── prompt_templates.json             # 模块化看图预设（SFW/NSFW/任务类型/缺口精简）
├── scripts/                              # 参数化 CLI 工具链（直接执行，勿手写样板脚本）
│   ├── common.py                         # 规则加载、规范化、原子落盘、per_image 增量、HTTP/curl 封装
│   ├── make_thumbs.py                    # 阶段 1：下采样（≤1536px / ≤2MB）
│   ├── build_tag_table.py                # 阶段 1.5：tag 动作表 + review_todo 缺口矩阵 + char_freq + per_image
│   ├── lookup_chars.py                   # 阶段 1.5b：Animadex 角色名录消歧（支持缓存与代理）
│   ├── parse_descriptions.py             # 阶段 2.5：严格 JSON / 旧文本解析 + 失败台账 + 裁切清理
│   ├── merge_captions.py                 # 阶段 3：三源/两源/纯视觉合并 + overrides + 分层自动裁剪
│   ├── validate_captions.py              # 阶段 4：Gate 1 离线（含 3 豁免）+ Gate 2 在线 3 态 & SQLite 缓存
│   └── apply_captions.py                 # 阶段 5：dry-run / 快照备份 / 原子写回 / caption_summary.json
├── assets/
│   ├── workflow.md                       # 执行详规（提示词模板、JSON 契约、轮转纪律、全部 CLI 示例）
│   └── normalization.md                  # 规则扩展（wash_rules.json）、否定式防护、冲突豁免与校验清单
├── references/                           # 按需加载参考文档
│   ├── Anima_style_LoRA_打标指南.md       # 核心打标规范
│   ├── target_types.md                   # 按目标类型（角色/画师/服装/物体/场景）与 WD14 标定
│   └── troubleshooting.md                # 排障手册、风格-内容纠缠处方与 Trainer 源码细节
└── tests/
    └── test_pipeline.py                  # 端到端与回归测试套件
```

## 快速开始

1. **安装 skill 与依赖**：将本仓库放入 `<skills-dir>/anima-style-lora-caption/`，安装基础依赖（核心流程仅需 Python ≥3.10 + `Pillow` + `pytest`）：
   ```bash
   pip install pillow pytest
   ```
2. **流水线总览（阶段 -1 ~ 5）**：
   - **阶段 -1**：归档原有 `.txt` 初稿，隔离非图文件，优先用 booru 元数据筛近重复。
   - **阶段 1**：`scripts/make_thumbs.py` 下采样缩略图。
   - **阶段 1.5**：`scripts/build_tag_table.py` 产出 `tag_table.csv`、`review_todo.csv`、`char_freq.csv` 并增量更新 `per_image.json`。
   - **阶段 1.5b**：`scripts/lookup_chars.py` 解析角色标准写法，交用户签字后注入视觉提示词。
   - **阶段 2.0 & 2**：先用 1~2 张图做冒烟探针，再按 `review_todo.csv` 缺口派发看图子代理；严格输出 `descriptions/<stem>.json`，teammate 每 5~8 张轮转重建。
   - **阶段 2.5**：`scripts/parse_descriptions.py` 解析输出、生成 `supplement.json` / `vision_failed.txt` / `vision_review.txt` 并清理残余裁切目录。
   - **阶段 3**：`scripts/merge_captions.py` 完成合并、规则清洗、`overrides.json` 覆盖与可选 `--trim-mode auto` 分层裁剪。
   - **阶段 4**：`scripts/validate_captions.py` 执行 Gate 1（标签计数不含 `@trigger`、3 类冲突豁免）与 Gate 2（在线 3 态 `VALID`/`INVALID`/`UNVERIFIED` + SQLite 缓存 + 别名建议）。
   - **阶段 5**：`scripts/apply_captions.py` 先 dry-run 门禁检查，传入 `--apply` 后快照备份并原子写回，最终产出 `caption_summary.json`。

各阶段完整 CLI 参数与提示词模板见 [assets/workflow.md](assets/workflow.md)。

## 两条铁律 & 核心经验

1. **主代理永不读图** —— 看图一律交子代理，避免图片挤爆主上下文。
2. **写一张落一张** —— 子代理逐张写入 `descriptions/<stem>.json`，严格 JSON 隔离标签数组与 `NOTE` 散文，避免括号逗号切碎与颜文字损坏。
3. **Teammate 轮转 + 批级抗拒答** —— 同一看图子代理每 5~8 张重建；遇整批拒答先缩小至 1 图 1 代理。
4. **失败记台账、`UNVERIFIED` 不降级** —— 视觉失败不静默兜底，网络未验证不降级为 `INVALID`。

## 运行测试

在仓库根目录运行：

```bash
pytest -v
# 或
python -m unittest discover -s tests -v
```

## 配套 Skill 与训练交接

`scripts/apply_captions.py` 写回完成后会在工作目录生成 **`caption_summary.json`**（汇总有效图数、`@trigger`、目标类型、标签分布与角色名录）。后续生成训练配置时，直接切换到配套 **`anima-lora-config`** skill 并传入该交接文件即可。

## 许可

MIT，见 [LICENSE](LICENSE)。
