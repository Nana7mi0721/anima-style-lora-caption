# 标签规范化与校验操作指南

配套 [SKILL.md](../SKILL.md) §6 与 [workflow.md](workflow.md) §3~§5。本文件说明**规则如何使用、如何按数据集扩展，以及离线/在线校验标准**；背后的标签取舍原则见 [Anima_style_LoRA_打标指南.md](../references/Anima_style_LoRA_打标指南.md)。

---

## 1. 规则单一事实源（`rules/`）

所有机器可执行规则统一维护在 `rules/` 目录，由 `scripts/common.py` 的 `load_rules()` 加载，**禁止在文档或脚本中手抄漂移副本**：

| 文件 | 职责与关键内容 |
|---|---|
| `rules/vision_fix.json` | 含 `aliases`（如 `pantsu`→`panties`、`topless`→`breasts out`、`front view`→`straight-on`、`chinese dress`→`china dress`）、`multi_split`（如 `bikini_pull`→`["clothes pull", "one breast out"]`）、`deprecated_drop`（`areola`/`areolae`/`bangs`）、`semantic_traps`（语义陷阱警告）、`color_downgrade`（如 `light blue hair`→`blue hair`、`golden eyes`→`yellow eyes`） |
| `rules/whitelist.txt` | 非 Danbooru 但对画风/构图可学的描写词白名单（每行一个小写短语，支持 `#` 注释），覆盖光影、构图与水印颜色词 |
| `rules/parent_fold.json` | `parent_to_children`（有具体子标签时折叠泛化父标签，如 `cat ears`→删 `animal ears`）与 `generic_drop_on_modifier`（并存 `white dress` 时删 `dress`） |
| `rules/banned.json` | `meta`、`quality`、`text_family`（已废弃的 `text`/`japanese text` 等整族）、`score_year_regex` |
| `rules/neg_keep.txt` | 合法的 Danbooru `no *` 否定前缀白名单（如 `no panties`、`no bra`、`no humans`、`no pupils`、`no shoes`、`no socks` 等） |
| `rules/slots.json` | `slot_order`（1~7）、`field_to_slot`（`POSE`→3、`WATERMARK`/`TEXT`→7）、`slot1_people`、`pose_tokens`、`composition_tokens`、`watermark_tokens`、`protected_tokens`、`trim_tiers`（1~4 级自动裁剪优先级）、`conflict_groups`、`conflict_exemptions` |
| `rules/prompt_templates.json` | 按 `task_type` × `rating` 及缺口预设拼装的模块化提示词配置 |

### 1.1 容易踩坑的关键规则

- **语义陷阱（必须显式核对）**：`cowgirl` 在 Danbooru 上有 active alias 指向 `cow girl`（牛仔女郎**服装**）。若语境为**性体位**，必须显式转为 `cowgirl position`，绝不能盲吃别名。一般原则：**吃 alias 前先确认语义**。
- **白名单必须早于角色前缀剥离**：口语清洗会剥离 `<角色名> <特征>`（如 `kanne brown eyes` → `brown eyes`）。`normalize_token()` 会先放行 `whitelist` 再执行前缀剥离，避免 `bright indoor light` 这类多词描写被误切；同时保留 `:3`、`^_^` 等颜文字并拦截括号注释残片（如 `man behind)`）。
- **父标签故意留白例外**：若刻意保留父标签（例如权威源具体种属不可靠，留给视觉层补全），需在 `tag_table.csv` 的 `note` 列注明原因，以免复查误判漏删。
- **解析与合并共用规则**：修改 `rules/` 或白名单同时影响 `parse_descriptions.py` 与 `merge_captions.py`，改后务必**先重跑解析再重跑合并**。

---

## 2. 数据集级扩展（`wash_rules.json`）

不污染全局 `rules/` 的前提下，可在工作目录放置数据集局部的 `wash_rules.json`，并通过 `--extra-rules <path>` 传给各脚本。`load_rules()` 的合并语义如下：

| 字段 | 合并语义 | 用途示例 |
|---|---|---|
| `aliases` | `dict.update()`（局部覆盖/追加全局） | 数据集专属错别字或特定词形映射 |
| `drop_exact` | 集合并集（`union`） | 仅在本数据集清除的特定噪声/高频干扰词 |
| `whitelist` | 集合并集（`union`） | 特定画风/场景才需要的额外白名单描写词 |
| `neg_keep` | 集合并集（`union`） | 数据集新增的合法 `no *` Danbooru 标签 |
| `drop_regex` | 列表追加（`append`） | 数据集专属的正则过滤规则 |

---

## 3. 否定式双层防护（升级版）

自动打标或看图子代理常输出 `no cum`、`tongue not out`、`no penetration`、`without clothing` 等「反向标签」。防护分两层：

1. **提示词层**：要求看不到的元素输出 `[]` / `none`，禁止写否定式。
2. **解析/合并层兜底**：匹配 `^(no|without|not)\b` 的 token 默认丢弃——**但在丢弃前强制核对双保险**：
   - 先查 `rules/neg_keep.txt`（已收编 `no panties`、`no bra`、`no humans`、`no pupils`、`no shoes` 等 24+ 合法 Danbooru 标签）；
   - 若提供了词典缓存/在线集合 `valid_dict`，再查 `to_danbooru_key(tag) in valid_dict`。命中任一即放行，确保 `no humans`/`no pupils`/`no shoes` 不再被误杀。

---

## 4. 标签计数口径、冲突检测与 3 条豁免

### 4.1 标签计数口径

**所有脚本与报告的标签数统计（推荐区间 20~45、`--min-tags`、`--max-tags`）一律严格排除首位 `@trigger`。** 例如 `@myartist, 1girl, solo` 计为 2 个标签，不再出现 off-by-one 偏差。

### 4.2 冲突检测与 3 条强制豁免

`scripts/validate_captions.py` 在 Gate 1 检查两类冲突：
1. **人数互斥**：`solo` 与 `solo focus` 互斥；`solo` 与 `1boy`/`2girls`/`3girls`/`multiple girls`/`multiple boys`/`hetero`/`yuri`/`group sex` 等多人/关系词互斥。
2. **同属性多值冲突**（`rules/slots.json` `conflict_groups`：`hair_color`、`eye_color`、`hair_length`），且强制执行以下 **3 条豁免**（`conflict_exemptions`）：
   - **豁免 1（多人图豁免）**：若 caption 含多人标签（`2girls`、`1girl`+`1boy`、`multiple girls` 等），跳过 `hair_color`、`eye_color`、`hair_length` 冲突检测（不同角色自然拥有不同发型发色）。
   - **豁免 2（多色/异色瞳豁免）**：含 `multicolored hair` / `two-tone hair` / `gradient hair` / `colored inner hair` 时豁免 `hair_color` 冲突；含 `heterochromia` / `gradient eyes` 时豁免 `eye_color` 冲突。
   - **豁免 3（`no panties` ≠ `no bra`）**：两者描写部位完全独立，**永远不列入同一互斥冲突组**。

---

## 5. 词典准备与 3 态在线校验

### 5.1 离线词典与形态转换

- **拉取排序**：从 Danbooru API 拉取离线词典时**必须按使用量降序**（`search[order]=count`）。默认按 id 倒序会抓到海量新建长尾词，几十万行也覆盖不到 `nude`/`1girl`。
- **双形态分离**：
  - **校验态**：使用 `to_danbooru_key()` 转为**下划线原形**（去转义括号 `\\(`），否则词典命中失败；
  - **落盘态**：使用 `norm_key()` 转为**空格小写**（仅 `score_*` 保留下划线），与 Anima 训练分布一致。

### 5.2 在线批量校验、SQLite 缓存与 3 态输出

`scripts/validate_captions.py --online` 调用 `https://danbooru.donmai.us/tags.json`：
- 参数：`search[name_comma]`（**逗号拼接**，非空格）、每批 `120` 个、`limit = len(batch) * 2`（防止 `limit=0` 只返回 1 条）。跳过 `@trigger`、whitelist 描写词与合法转义角色名。
- 支持 `--proxy <url>`、必要时 `--use-curl`（走 `curl -x`）以及 `--auth user:key`（或环境变量 `DANBOORU_LOGIN` / `DANBOORU_API_KEY`）。
- **持久化 SQLite 缓存（`--cache danbooru_cache.sqlite`）**：已确认查询按 TTL（如 30 天）落盘，复跑直接命中缓存，避免重复请求。
- **严格 3 态判定**：
  - `VALID`：`post_count > 0` 且 `is_deprecated == false`
  - `INVALID`：网络正常返回且确认 `post_count == 0` 或 `is_deprecated == true`
  - `UNVERIFIED`：网络/HTTP/代理异常且重试耗尽——**绝不降级为 `ABSENT`/`INVALID`**。`--strict` 模式与 `apply_captions.py` 默认都会因 `UNVERIFIED > 0` 阻断写回。
- **别名自动建议（`--suggest-aliases`）**：对 `INVALID` 标签批量查 `tag_aliases.json`（`search[antecedent_name]`、`search[status]=active`）给出替换建议；若命中 `rules/vision_fix.json` 的 `semantic_traps`（如 `cowgirl`），显式标红提醒人工审阅，禁止盲目替换。

---

## 6. 离线 Gate 1 完整断言清单

- [ ] 图-txt 1:1 配对完整（无漏无多余）
- [ ] 单行、**UTF-8 无 BOM**、**无末尾换行符**
- [ ] 无前导逗号、无尾标点、半角 `, ` 分隔
- [ ] 全小写空格形（`score_*` 例外）、无重复 token
- [ ] `@trigger` 置首，且 `@` 仅出现在首位（除非明确登记）
- [ ] 标签数（**不含 `@trigger`**）落在目标区间（默认 20~45）
- [ ] `BANNED` 零残留（画师/社团/IP 系列/meta/质量词/`text` 族/`score_*`/`year_*`）
- [ ] 非法否定式零残留（`neg_keep.txt` 与词典合法词除外）
- [ ] 人数冲突 0，且属性冲突检查已应用 3 条强制豁免
- [ ] 水印区（Slot 7）与视觉台账/补充结果逐张一致（不作恒定存在假设）
- [ ] 近重复/差分对标签保持一致性
