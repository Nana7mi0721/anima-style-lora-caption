# 打标执行流程详规

配套 [SKILL.md](../SKILL.md) §3~§5。本文件定义**端到端执行步骤、提示词契约与 CLI 调用**；规则定义在 `rules/`（操作细则见 [normalization.md](normalization.md)），标签取舍见 [Anima_style_LoRA_打标指南.md](../references/Anima_style_LoRA_打标指南.md)。

> 全文使用 `<READ_IMAGE_TOOL>` 与 `<WRITE_TOOL>` 作为工具占位符；派发子代理前替换为当前宿主环境实际探测到的工具名（如 `read_image`/`Read`、`write_file`/`Write`）。

---

## 1. 阶段 -1 ~ 1.5b：预检、下采样与视觉前准备

### 1.1 阶段 -1：归档初稿与轻量预检

1. **原样归档初稿**：自动打标初稿（JoyCaption / WD14 / VLM）与最终 caption 通常同名（`<stem>.txt`），写回会直接覆盖。开工前将现有 `.txt` 原样复制到 `<WORK>/drafts_archive/`（保留前导 `, ` 等原始瑕疵，剥除留到后续阶段）。
2. **清理非图文件**：扫描数据集目录，将 `.zip`、`.mp4`、`.gif`、`.psd` 等非静态图像隔离或移出训练目录（`scripts/common.py` 的 `iter_images` 仅扫描 `.png`/`.jpg`/`.jpeg`/`.webp`，并跳过 `_`/`.*`/`thumbs`/`masks`/`backups` 目录）。
3. **近重复轻量预检**：先于像素比对，优先利用 booru 元数据的 `parent_id`、`pixiv_id` 与 tag Jaccard 相似度筛出同源图/差分图，同时检查 md5 字节重复（判断标准见 `Anima_style_LoRA_打标指南.md` §8.5）。
4. **水印/文字策略定调**：按指南 §8.1 决策树确认「能去则图层面去掉，不能去则按视觉事实标入槽位 7」。

### 1.2 阶段 1：下采样（`scripts/make_thumbs.py`）

统一尺寸至长边 ≤1536 px、体积 ≤2 MB（RGBA 自动铺白底转 RGB，自动处理 EXIF 旋转），规避大图静默拒答与请求体超限：

```bash
python scripts/make_thumbs.py \
  --src "<DATASET_DIR>" \
  --dst "<WORK>/thumbs" \
  --long-edge 1536 \
  --max-mb 2.0 \
  --quality 85 \
  --recursive
```

- 默认跳过已存在缩略图（加 `--force` 强制重生成；调试可用 `--limit 2`）。
- 原图普遍 ≤3.5MB 时可跳过此步直接读原图；一旦下采样，子代理的裁切坐标一律基于缩略图。

### 1.3 阶段 1.5：动作表、缺口矩阵与增量批入库（`scripts/build_tag_table.py`）

扫描数据集图片、归档初稿与已有元数据，一次性生成 3 张表并**增量追加** `per_image.json`：

```bash
python scripts/build_tag_table.py \
  --dataset "<DATASET_DIR>" \
  --out-table "<WORK>/tag_table.csv" \
  --out-todo "<WORK>/review_todo.csv" \
  --out-chars "<WORK>/char_freq.csv" \
  --per-image "<WORK>/per_image.json" \
  --drafts-dir "<WORK>/drafts_archive" \
  --metadata-csv "<WORK>/booru_metadata.csv" \
  --min-tags 20 \
  --min-char-freq 4 \
  --trigger "@mytrigger"
```

| 产物 | 用途 |
|---|---|
| `tag_table.csv` | `tag,count,action,target,slot,note`；规则变动时改表重跑合并，不必重跑视觉 |
| `review_todo.csv` | **缺口驱动派发矩阵**：`stem,image,has_a_source,tag_count,missing_slots,conflicts,priority,preset` |
| `char_freq.csv` | 候选角色频次表（`tag,count,meets_threshold`），直接喂给阶段 1.5b |
| `per_image.json` | **只追加不截断（append/upsert）**：新批次入库时保留全部既有 stem 的历史标注与元数据 |

### 1.4 阶段 1.5b：视觉前角色名录消歧（`scripts/lookup_chars.py`）

在派发看图前先确定出场角色标准写法，再注入看图提示词：

```bash
python scripts/lookup_chars.py \
  --char-freq "<WORK>/char_freq.csv" \
  --min-freq 4 \
  --out "<WORK>/char_roster.json" \
  --cache "<WORK>/animadex_cache.jsonl" \
  --proxy "http://127.0.0.1:7890"
```

- 优先查 Animadex 取 `trigger` 首段（去掉版权后缀），支持 `--use-curl`、`--local-dict` 与 `--offline` 缓存模式。
- 若有 Pixiv / FANBOX 投稿标题或作品目录，结合标题补充候选角色并核对。
- **把生成的角色名录表交用户签字确认**，确认后的 `char_roster.json` 注入阶段 2 的 `CHARACTER` 提示词并传给 `merge_captions.py`。

---

## 2. 阶段 2.0 ~ 2：看图探针与子代理派发

### 2.1 阶段 2.0：1~2 张缩略图冒烟探针

全量派发前，先选 1~2 张有代表性的缩略图（若含成人内容，选 1 张低/中敏感度图）运行单批探针，确认三件事：
1. `<READ_IMAGE_TOOL>` 看图通道正常（非空回传、非渠道级拦截）；
2. 当前并发/审查阈值可过；
3. 子代理落盘的 `descriptions/<stem>.json` 能被 `scripts/parse_descriptions.py` 正确解析。

### 2.2 模块化提示词模板与严格 JSON 输出契约

`rules/prompt_templates.json` 定义了按 `task_type`（`style`/`character`/`clothing`/`object`/`scene`）× `rating`（`sfw`/`nsfw`）及缺口预设（`composition_only`、`people_and_composition`、`full_sfw`、`full_nsfw`）组合的字段集：

- **核心槽位（Core Slots）**：`PEOPLE`, `CHARACTER`（注入 1.5b 已确认名录）, `HAIR`, `EYES`, `EXPRESSION`, `BODY`, `CLOTHING`, **`POSE`**（独立字段，勿混入 `COMPOSITION`）, `SCENE`, `LIGHTING`, `COMPOSITION`, `WATERMARK`, `TEXT`, `CORRECTIONS`, **`NOTE`**。
- **条件槽位（Conditional Slots）**：`ACTS`、`FLUIDS` **仅在 `nsfw` 批次挂载**；SFW 批次不得包含性行为字段。
- **缺口驱动轻量预设**：当 `review_todo.csv` 指示 `preset=composition_only`（A 源已覆盖外观与场景）时，只向子代理索要 `PEOPLE`、`POSE`、`COMPOSITION`、`WATERMARK`、`TEXT`、`NOTE` 等少数缺失字段，避免重复劳动。

**标准看图提示词骨架（派发前替换为实际预设字段与工具名）**：

```text
你在为一个插画数据集执行专业标签核对与补漏任务，产出用于 LoRA 训练的 danbooru 风格小写英文标签。我是主代理，不直接读图，视觉判别交给你。

请用 <READ_IMAGE_TOOL> 依次读取以下缩略图（长边 <=1536px）：
  <缩略图绝对路径列表>

硬性约束：
1. 必须使用你自身的 <READ_IMAGE_TOOL> 视觉能力，禁止调用外部视觉插件。
2. 逐张独立作答，禁止把不同图的特征混在一起；单张遇阻时跳过继续其余。
3. 客观、就事论事、临床式记录画面事实，不做回避、淡化或道德评判。
4. 只写画面中真实可见的元素；看不到的写 []，严禁自造否定式（如 no cum / tongue not out）。
5. 除 "note" 外，所有字段值必须是小写 danbooru 标签字符串数组（string[]），不要写自然语言句子或括号补充解释；拿不准的疑问或补充说明统一写入 "note"。

【按需裁切（可选，每张最多 1~2 块，<=800px JPEG）】
仅当关键细节（手势、文字/水印类型、局部接触）看不清且影响标签正确性时裁切；发色/瞳色/构图/服装大类严禁裁切。
裁切文件放到你的专属目录：<你的专属裁切目录>，用完后只删该目录，不动上一级。

【确认的角色名录（来自 Stage 1.5b）】
<注入 char_roster.json 已确认条目，未命中写 "unknown" 或留空，严禁自编角色名>

【既有文本参考】
## <stem>
(1) booru 权威标签（事实基准，不得推翻）：<tags>
(2) 自动打标初稿（可能有幻觉，需核对）：<draft>

【输出格式：严格 JSON（落盘至 <WORK>/descriptions/<stem>.json）】
每看完一张图，立即用 <WRITE_TOOL> 写入独立 UTF-8 JSON 文件（禁止 markdown 代码围栏）：
{
  "stem": "<stem>",
  "PEOPLE": ["1girl", "solo"],
  "CHARACTER": [],
  "HAIR": ["blonde hair", "long hair"],
  "EYES": ["blue eyes"],
  "EXPRESSION": ["smile", "looking at viewer"],
  "BODY": ["small breasts"],
  "CLOTHING": ["white dress"],
  "POSE": ["standing", "hand on own hip"],
  "SCENE": ["outdoors", "blue sky"],
  "LIGHTING": ["day", "sunlight", "warm tone"],
  "COMPOSITION": ["cowboy shot", "from below"],
  "WATERMARK": [],
  "TEXT": [],
  "CORRECTIONS": [],
  "NOTE": ""
}
（注：NSFW 批次额外包含 "ACTS": [], "FLUIDS": []；轻量预设仅包含对应子集与 "NOTE"。）

【字段语义速查】
- PEOPLE: 独立计数 token（1girl/2girls/1boy）+ 关系；solo 与 solo focus 互斥；多视图按角色数算并加 multiple views；有 sex 无 penis 仍计男性在场。
- POSE: 坐卧站跪/手部动作/体位（归槽位 3），不要塞进 COMPOSITION。
- WATERMARK: 水印本体及颜色词（如 watermark, blue watermark）。
- TEXT: 画面非水印文字元素（speech bubble / sound effects / signature / artist name / logo），禁止使用已废弃的 text 族词。
- CORRECTIONS: 初稿中与画面明显不符的误标 token。
- NOTE: 唯一允许写自然语言说明、不确定项或裁切说明的字段。

【收尾与回复】
删掉自己的裁切目录。正文仅回复状态行，不要复述标签：
  <stem> ok
  <stem> refused: <一句话原因>
```

> **为什么用严格 JSON + `NOTE` 隔离**：旧版自由文本 `KEY: val` 遇到括号注释（如 `solo (man behind)`）或颜文字（`:3`）时极易在逗号切分阶段产生残片垃圾 token；严格 JSON 把标签锁定在 `string[]`、把解释隔离进 `NOTE`，同时保留「表单填表」抗拒答框架。（解析脚本仍向后兼容旧版 `KEY: val` `.txt`。）

### 2.3 字段 → 槽位显式映射（对应 `rules/slots.json`）

| 提示词字段 | 目标槽位 | 说明 |
|---|---|---|
| `PEOPLE`, `CHARACTER` | **Slot 1**（主体） | 人数、关系、已确认角色名 |
| `HAIR`, `EYES`, `EXPRESSION`, `BODY`, `CLOTHING` | **Slot 2**（外观着装） | 视线与表情归 Slot 2 |
| **`POSE`**, `ACTS`, `FLUIDS` | **Slot 3**（姿势动作） | 姿态与性描写同归 Slot 3 |
| `SCENE` | **Slot 4**（场景环境） | 地点、道具、背景 |
| `LIGHTING` | **Slot 5**（光影色调） | `day`/`night`/白名单光影词 |
| `COMPOSITION` | **Slot 6**（构图镜头） | 景别、视角、焦距 |
| **`WATERMARK`, `TEXT`** | **Slot 7**（水印/文字元素） | 水印与对话框/签名/logo 同归 Slot 7 |
| `CORRECTIONS`, `NOTE` | `null`（仅元数据） | 不直接并入最终 caption |

### 2.4 裁切、并发分组与 DSH Teammate 轮转上限

| 项 | 规定 |
|---|---|
| **Teammate 轮转上限** | **每处理 5~8 张图必须重建/轮转子代理**，防止历史图片 token 挤爆上下文并杜绝跨图特征漂移 |
| **裁切纪律** | 每图 ≤2 块、JPEG ≤800px、每原图只读 1 次、专属子目录用完自删 |
| **并发与分批** | 先小批试探渠道并发上限；整波取消时拆小波次 |
| **敏感度与类型隔离** | SFW 与 NSFW **绝不混编**同一子代理；成人图按低→中→高分级；商业封面单独成组 |
| **近重复隔离** | 近重复对**不派给同一子代理**，防印象跨图复制 |
| **断点续派** | 生成派发清单时扫描磁盘 `descriptions/`，自动跳过已落盘 stem |

### 2.5 失败台账与拒答排查

失败写入 `vision_failed.txt`（格式 `<stem>\t<reason>`），**只记台账、不兜底、不静默退回文本源**：

- 原因枚举：`no_vision_input` / `no_description_file` / `subagent_empty` / `refused` / `no_block` / `parse_empty`
- **整批拒答**：缩粒度至 **1 图 1 代理**（最有效，拒绝往往是批次级触发）
- **仍硬拒**：换子代理模型/渠道；或运行四段二分探针（纯模板 → 纯初稿 → 单张新图 → 单张历史成功图）定位是否渠道策略收紧
- **备用视觉工具**：若子代理视觉均不可用，主代理可通过只回传文本的第三方视觉工具**严格单发**补标，注意其对露骨字段可能净化，需在 `NOTE` 标注来源

---

## 3. 阶段 2.5：解析（`scripts/parse_descriptions.py`）

将子代理落盘的 `.json`（或兼容旧版 `.txt`）解析为统一槽位结构，执行规范化初筛，并自动清理残余裁切目录：

```bash
python scripts/parse_descriptions.py \
  --desc-dir "<WORK>/descriptions" \
  --dataset "<DATASET_DIR>" \
  --out-json "<WORK>/supplement.json" \
  --out-failed "<WORK>/vision_failed.txt" \
  --out-review "<WORK>/vision_review.txt" \
  --tag-table "<WORK>/tag_table.csv" \
  --crop-root "<WORK>/crops" \
  --thumbs-dir "<WORK>/thumbs"
```

- 解析层自动防护：括号注释残片拦截、`:3`/`^_^` 颜文字保护、`rules/whitelist.txt` 优先于角色前缀剥离、误填进 `COMPOSITION` 的标准 `pose_tokens` 自动救援回 Slot 3。
- 产出 `supplement.json`、`vision_failed.txt` 与 `vision_review.txt`（未在动作表/标准集合登记的新 token 供人工核对）。

---

## 4. 阶段 3：合并与分层裁剪（`scripts/merge_captions.py`）

```bash
python scripts/merge_captions.py \
  --mode three-source \
  --trigger "@mytrigger" \
  --dataset "<DATASET_DIR>" \
  --out-dir "<WORK>/staged_captions" \
  --tag-table "<WORK>/tag_table.csv" \
  --supplement "<WORK>/supplement.json" \
  --per-image "<WORK>/per_image.json" \
  --drafts-dir "<WORK>/drafts_archive" \
  --overrides "<WORK>/overrides.json" \
  --char-roster "<WORK>/char_roster.json" \
  --max-tags 45 \
  --trim-mode auto \
  --target-type style
```

- **三种合并模式**：`three-source`（A 基准 + B 补缺 + C 仅校验）、`two-source`（B 为骨 + C 核对）、`vision-only`（仅 B）。
- **核心动作**：应用 `rules/` 规范化 → 父标签折叠（`rules/parent_fold.json`）→ 同图多发色自动补 `multicolored hair` → 水印台账对齐（无水印则省略 Slot 7）→ 1~7 槽位排序 → `@trigger` 置首去重。
- **声明式手工覆盖 `overrides.json`**：合并最后阶段幂等执行，重跑合并绝不丢失人工微调。格式：
  ```json
  {
    "0001": { "add": ["mole under eye"], "remove": ["blush"], "lock": false },
    "0002": { "lock": true, "caption": "@mytrigger, 1girl, solo, ..." }
  }
  ```
  `lock: true` 且提供 `caption` 时原样锁定；否则先 `remove` 后 `add` 并重排槽位。
- **分层自动裁剪（`--trim-mode auto`）**：当不含 `@trigger` 的标签数 > `--max-tags`（默认 45）时，按 `rules/slots.json` 的 `trim_tiers`（Tier 1 → Tier 4）依次裁剪低优先级修饰词，**永久保护** `protected_tokens`、`@trigger`、角色名与 `overrides.add` 标签，并在 `merge_report.json` 中记录每张图的裁剪明细（默认 `--trim-mode warn` 仅告警不裁）。

> ⚠️ **联动纪律**：修改 `rules/` 或数据集 `wash_rules.json` 后，**必须先重跑 `parse_descriptions.py` 再跑 `merge_captions.py`**，否则词已在解析层被过滤。

---

## 5. 阶段 4：两道校验（`scripts/validate_captions.py`）

```bash
python scripts/validate_captions.py \
  --captions-dir "<WORK>/staged_captions" \
  --dataset "<DATASET_DIR>" \
  --trigger "@mytrigger" \
  --min-tags 20 \
  --max-tags 45 \
  --watermark-ledger "<WORK>/supplement.json" \
  --online \
  --cache "<WORK>/danbooru_cache.sqlite" \
  --batch-size 120 \
  --proxy "http://127.0.0.1:7890" \
  --suggest-aliases \
  --out-report "<WORK>/validation_report.json" \
  --strict
```

- **Gate 1（离线）** 与 **Gate 2（在线 3 态 `VALID`/`INVALID`/`UNVERIFIED` + SQLite 缓存 + `--use-curl` + 别名建议）** 的详细判据与 3 类冲突豁免见 [normalization.md](normalization.md) §4~§6。
- `--strict` 在存在离线错误或 `UNVERIFIED > 0` 时返回非零退出码，防止未验证数据集直接写回。

---

## 6. 阶段 5：快照备份、原子写回与交接（`scripts/apply_captions.py`）

默认处于 **dry-run**，确认报告无误后追加 `--apply` 执行真实写回：

```bash
# 1. Dry-run 预检
python scripts/apply_captions.py \
  --staged-dir "<WORK>/staged_captions" \
  --dataset "<DATASET_DIR>" \
  --backup-dir "<BACKUP_DIR>" \
  --trigger "@mytrigger" \
  --target-type style \
  --validation-report "<WORK>/validation_report.json" \
  --char-roster "<WORK>/char_roster.json" \
  --out-summary "<WORK>/caption_summary.json"

# 2. 真实写回
python scripts/apply_captions.py ... --apply
```

- **强前置门禁**：若 `validation_report.json` 存在 Gate 1 错误或 `online_unverified > 0`（除非显式传入 `--force-unverified`），脚本拒绝写回。
- **备份范围**：先将数据集内原有 `.txt` 初稿（原字节保留）与元数据完整快照至 `<BACKUP_DIR>/<dataset>_<YYYYMMDD_HHMMSS>/`，再执行同目录临时文件 + `os.replace` 原子写回，并对磁盘最终 `.txt` 再跑一次格式复检（单行、UTF-8 无 BOM、无尾换行）。
- **下游交接**：产出 `caption_summary.json`（含张数、触发词、目标类型、标签分布、角色名录摘要），直接供 `anima-lora-config` 生成训练配置。

### 6.1 改规则后的快速迭代与中断恢复

- **改规则重跑**：修改 `tag_table.csv` / `wash_rules.json` / `overrides.json` → 重跑 `parse_descriptions.py`（若改了规则/白名单）→ `merge_captions.py` → `validate_captions.py` → `apply_captions.py --apply`。无需重跑看图。
- **中断恢复**：子代理落盘 `descriptions/<stem>.json`，重派时自动跳过已落盘图；`per_image.json` 只追加不截断；残余裁切目录由 `parse_descriptions.py` 清理；主会话重启后直接读取磁盘状态续跑。
