# Anima LoRA 打标指南（通用版）

> 本篇定义**标签内容规范**（格式、槽位、取舍、规范化原理与校验标准）。
> 执行流程见 [workflow.md](../assets/workflow.md)；操作细则见 [normalization.md](../assets/normalization.md)；机读规则以 [slots.json](../rules/slots.json) 等 `rules/` 文件为唯一权威源（SSOT）。版本：2026-09

---

## 目录

1. [基础事实：Anima 吃什么 Caption](#一基础事实anima-吃什么-caption)
2. [核心心智模型：该标什么、不该标什么](#二核心心智模型该标什么不该标什么)
3. [Caption 格式硬约束](#三caption-格式硬约束)
4. [槽位结构与顺序](#四槽位结构与顺序)
5. [按训练目标的打标差异（总表）](#五按训练目标的打标差异总表)
6. [标签来源与三源优先级](#六标签来源与三源优先级)
7. [标签规范化原则（SSOT 索引）](#七标签规范化原则ssot-索引)
8. [专项条款](#八专项条款)
9. [校验与排障入口](#九校验与排障入口)
10. [附录：模板、合规范例与速查](#十附录模板合规范例与速查)

---

# 一、基础事实：Anima 吃什么 Caption

## 1.1 官方标签顺序与评级映射

Anima 官方模型卡规定的标签段顺序：

```text
[quality/meta/year/safety] [1girl/1boy/1other] [character] [series] [artist] [general tags]
```

- 全半角逗号分隔、**全小写**、**空格而非下划线**（`score_7` 等评分标签除外）。
- **画师标签必须加 `@` 前缀**（官方原文：「不加 `@` 效果会非常弱」）。
- 训练混合纯标签 / NL / 混排并含随机 tag dropout ⇒ **不要求写全所有相关标签**。

**安全/评级词汇是 Anima 自定词，非 Danbooru 词**：`general`→`safe`、`sensitive`→`sensitive`、`questionable`→`nsfw`、`explicit`→`explicit`。若写评级，**只写一次**置于 `[safety]` 段并从 general 串剔除裸的 `questionable`/`general`；角色 LoRA 可全部省略。

## 1.2 Trainer 硬约束（6 行摘要）

已对照 Anima-Standalone-Trainer 源码核实（源码行号与 SDXL 差异见 [troubleshooting.md](./troubleshooting.md)）：

1. **只读首行**：`train_util.py` 按 `\n` 截断取首行，第二行起静默丢弃 ⇒ **必须单行**。
2. **禁止 `shuffle_caption`**：开启 TE 缓存时，`shuffle_caption` / `token_warmup_step` / `caption_tag_dropout_rate` 触发断言中止 ⇒ **shuffle 必须 false**。
3. **按 `,` 切分**：`caption_separator` 默认 `","`；混排 NL 句内逗号会被切碎。
4. **subset dropout 上提**：`caption_dropout_rate` 取全局最大并迁至 embedding 级，兼容 TE 缓存。
5. **Token 上限 512**：Qwen3/T5 默认各 512；20~45 标签远未触顶，**不要写长散文**。
6. **稳定锚点**：关掉 shuffle 后，**`@触发词` 置首 + `keep_tokens=1`** 防顺序漂移。

---

# 二、核心心智模型：该标什么、不该标什么

## 2.1 第一原则（Caption 悖论）

> **想用 prompt 独立控制的东西 → 写进 caption；想让触发词自己吸收的东西 → 不写。**

- **变化因素逐张写**（姿势/服装/表情/场景/镜头/光照），漏写会被当固有特征绑进触发词。
- **固有特征按目标决定**：角色 LoRA 发/瞳可写可不写；画风 LoRA **绝不写「风格」词**，内容全写以解耦。
- **看不到的不写**：留空，**绝不写杜撰否定式**（§7.4）。

## 2.2 过标、欠标与标签数区间

- **过标**（60+ 词、堆泛化父标签）：稀释信号；**欠标**（仅 `@trigger, solo, sitting`）：未标常量被绑死。
- **口诀**：删掉该词若不改变别人怎么用你的 LoRA，就质疑它该不该在。
- 实测成品均值 **30.8~38.6** ⇒ **目标区间 20~45 个标签（严格不含首位 `@触发词`，与 [validate_captions.py](../scripts/validate_captions.py) / [merge_captions.py](../scripts/merge_captions.py) 一致）**；超 45 可用 `merge_captions.py --trim-mode auto` 按 [slots.json](../rules/slots.json) `trim_tiers` 分级裁剪。

## 2.3 该删 vs 该留（视觉对应物准则）

| 类别 | 处置 | 例子与依据 |
|---|---|---|
| 画师/社团/IP 系列名 | **删** | `someartist_(handle)`、`touhou`、`blue_archive`（永不加 IP 名） |
| meta / 媒介 / 质量常量 | **删** | `uncensored`、`highres`、`absurdres`、`translated`（无像素对应物；质量词仅逐张测量可用） |
| 低频角色名 | **删** | 出现 &lt;4 次（§8.2） |
| 已废弃文字族 / 杜撰否定 / 散文 | **删** | `text`/`japanese text`、`no cum`、`looking at the scene`、`very aroused` |
| **有固定像素对应物的常量** | **留** | `watermark`（防学成画风）、`wet`（视觉事实）、`nipples`（不做道德删减） |

---

# 三、Caption 格式硬约束

写回前须通过 [validate_captions.py](../scripts/validate_captions.py) 全量机检：

| # | 约束 | 依据 |
|---|---|---|
| 1 | **单行** | `train_util.py` 只取首行 |
| 2 | **全小写** | 官方模型卡 |
| 3 | **空格，不用下划线**（`score_*` 例外） | 同集混用等于把概念拆半 |
| 4 | **分隔符 = 半角 `, `** | 全角逗号会切分失败 |
| 5 | **UTF-8 无 BOM、无尾换行** | 读写与校验脚本硬规范 |
| 6 | **无前导逗号、无尾标点** | 剥除 `{trigger}, {output}` 残留 |
| 7 | **触发词置首且唯一 `@`** | 除登记带 `@` 角色名外，`@` 仅在首位 |
| 8 | **无重复 token** | 重复标签稀释信号 |
| 9 | **无杜撰否定式** | 合法 `no ` 标签由 [neg_keep.txt](../rules/neg_keep.txt) 放行 |
| 10 | **无质量词**（除非逐张测量） | 常量质量词制造噪声 |
| 11 | **数据集内形态统一** | 防 `black_hair` 与 `black hair` 并存 |
| 12 | **标签数 20~45（不含 `@触发词`）** | 超限可用 `trim_tiers` 分级裁剪 |

> **混排 NL（非默认）**：若确需补充标签无法表达的空间/光照方向，用 `. ` 接在标签段后同处一行（1~3 句），不得复述已有标签。

---

# 四、槽位结构与顺序

## 4.1 七槽位顺序（[slots.json](../rules/slots.json)）

```text
@触发词, [1 主体] 人数 + 关系 + 角色名
→ [2 外观着装] 发色/发型/发长 → 瞳色 → 表情/视线 → 身材 → 服装/饰品 → 身体特征
→ [3 姿势动作] 姿势/体位/手部动作/性描写/生殖器/体液
→ [4 场景环境] 地点/道具/背景
→ [5 光影色调] day/night/sunlight/warm tone/cool tone…
→ [6 构图镜头] upper body/cowboy shot/from above/close-up/full body…
→ [7 水印区] watermark / 颜色词 / signature / logo（无则整区省略）
```

输出**不带槽位标记**。将 `@触发词` 置首配合 `keep_tokens=1`，其余与官方 `[count] [character] [general]` 对齐。

## 4.2 槽位 2 / 3 分界与看图必答字段

- **槽位 2**：发/瞳/**表情**/**视线**/体型/服装/饰品/身体特征（`nipples`、`breasts out`、`no panties`、`wet`、`sweat`）。**视线（如 `looking at viewer`）归槽位 2**。
- **槽位 3**：姿势/体位/手部动作/性行为/生殖器/体液（`pussy`、`penis`、`anus`、`cum`、`spread legs`、`on back`）。
- **来源覆盖实测**：A 缺人数/构图(0)/光影(0)/体位细节；C 场景 99%、光影 96%，但人数 25%、构图 7%、服装漏 1/3 ⇒ **看图必答字段锁定在人数、关系、构图、体位细节**。

---

# 五、按训练目标的打标差异（总表）

角色特征 `>=0.9` 剪切、画风解耦、服装四层分离、物体/场景要点及 WD14 标定见 [target_types.md](./target_types.md)。

| 维度 | 人物/角色 | 画师风格 | 服装 | 物体/概念 | 场景/环境 |
|---|---|---|---|---|---|
| **角色 tag** | 删（触发词替代） | 通常不写（高频交用户裁决） | 写 | — | — |
| **固有特征** | 可写可不写（≥0.9 可剪） | 不写「风格」，内容全写 | 角色与服装全写 | 逐张写 | 逐张写 |
| **触发词 `@`** | 可 `@` 或裸词 | **必须 `@`** | 可 `@` | 可 `@` | 可 `@` |
| **核心要求** | 表情/视线/姿势/背景必写 | 全部内容解耦 | 人/衣/姿/景四层分离 | 状态/朝向/使用中 | 构图/景别/时间/天气 |
| **质量词 / 多样性** | 不写 / 中 | 不写 / **极高** | 不写 / 高 | 不写 / 中 | 不写 / 高 |

---

# 六、标签来源与三源优先级

- **A（booru 权威标签）**：事实基准，A 已覆盖字段不得推翻。
- **B（图片看图）**：视觉真相；有 A 时补空缺，无 A 时 **B 为骨架**。
- **C（自动初稿）**：待校验输入，与 A/B 冲突一律判幻觉丢弃。常见坑：前导 `, `、凭空幻觉或整张错位、人数构图极缺、裁图去字后旧稿失效、杜撰否定式与口语自造词。

---

# 七、标签规范化原则（SSOT 索引）

完整映射表与清单已收敛至 `rules/` 与 [normalization.md](../assets/normalization.md)，本节只说明**原则与依据**：

1. **词典与在线三态闸门**：Danbooru `tags.json` 拉取**必按使用量降序**（否则抓几十万行也抓不到 `1girl`/`nude`）；校验用下划线原形，落盘用空格形。在线 `search[name_comma]`（120/批，`limit=len*2`）严格区分 **`VALID`** / **`INVALID`** / **`UNVERIFIED`**（网络失败绝不降级判 `INVALID`）。
2. **别名语义陷阱与废弃拆分**（[vision_fix.json](../rules/vision_fix.json)）：别名只保词形合法，不保语义正确——如 `cowgirl` 指向服装 `cow girl`，性体位必须显式改写为 `cowgirl position`。废弃词不可盲删信息（`topless`→`breasts out`，`bikini_pull`→`clothes pull`+`one breast out`；`areola`/`bangs` 才直接删除）。
3. **颜色降级、父标签折叠与冲突豁免**（[parent_fold.json](../rules/parent_fold.json)、[slots.json](../rules/slots.json)）：词典无的复合色降泛色（`light blue hair`→`blue hair`、`silver hair`→`white hair`、`golden/amber eyes`→`yellow eyes`）；具体色与泛形并存时留具体形（`white dress` 折叠 `dress`）；具体子类折叠泛化父标签（`cat ears` 折叠 `animal ears`）。冲突检测三项豁免：**多人图跳过**、**`multicolored hair`/`heterochromia` 类跳过对应颜色组**、**`no panties` 与 `no bra` 彼此独立不冲突**。
4. **否定式零容忍与合法 `no ` 保护**（[neg_keep.txt](../rules/neg_keep.txt)、[banned.json](../rules/banned.json)）：杜撰否定式（`no cum`/`tongue not out`/`no penetration`）提示词层与合并层双封杀；Danbooru 自身合法词（`no panties`、`no bra`、`no humans`、`no pupils`、`no shoes` 等）经 `neg_keep.txt` 与词典核验予以保留。
5. **白名单与「posts=0 = 噪声」**（[whitelist.txt](../rules/whitelist.txt)）：`left side ponytail` 在 Danbooru posts=0，底模当噪声；方向信息改由可选 NL 补充。非 Danbooru 的明确光影/构图/水印色词入白名单，且**必须在角色名前缀剥离前放行**（改白名单后须重跑解析再合并）。

---

# 八、专项条款

## 8.1 水印 / 签名 / 残留文字

能图像层去掉/裁掉的先去除；去不掉的**有固定视觉对应物**，按实际可见性标入槽位 7（否则水印会被学成画风）：水印标 `watermark`（可加颜色词），可见签名标 `signature` + `artist name`，商业封面标 `logo`，空对话框标 `speech bubble`，拟态词标 `sound effects`；已废弃 `text` 族永不写。若仅部分图有水印，**按视觉台账逐张标**，切勿写成全集恒定断言。

## 8.2 角色 tag 阈值与拼写来源

- **频次阈值 ≥4**：出现 1~3 次的客串角色一律不写；≥4 次在画风集默认保留并交用户裁决。
- **拼写优先查 Animadex**（[lookup_chars.py](../scripts/lookup_chars.py)）：取 `trigger` 首段去版权后缀，注意 `gotou_hitori`→`gotoh hitori`、`fern` 带限定词而 `frieren` 不带等差异；查不到回退本地词典。角色 LoRA 则**删除原角色名**，由触发词承载。

## 8.3 人数与关系（必须看图）

不可靠文本猜人数，不自动兜底 `1girl`。数量写独立 token（`2girls, 1boy`）；关系词（`multiple girls`、`solo focus`、`hetero`、`yuri`、`group sex`、`faceless male`）同归槽位 1。**硬互斥**：`solo` 与 `solo focus` 及所有多人/多性向标签互斥。有 `sex` 无 `penis` 仍可能有人在场（补 `faceless male`）；同一角色多视图按实际人数标并加 `multiple views`，勿把分镜数当人数。

## 8.4 NSFW / 成人内容

临床式客观直写，不做道德评判或删减。booru 只打大类，`spread legs`、`on back`、`mating press`、`after sex`、`cumdrip` 等体位/体液细节须看图补全进槽位 3。可见遮蔽（`nipples`、`censored`、`mosaic censoring`、`bar censor`）保留；`uncensored` 删除。误报拒答仅记台账。

## 8.5 近重复图

**先查元数据，后看像素**：有 booru 元数据时优先用 `parent_id`/`pixiv_id` 与标签 Jaccard 相似度筛候选对，再核对 MD5/像素。同图缩放/裁剪/水印重压版**删除**；差分对保留但须**差分一致性**（非差分标签全同）；视觉近似对登记并**拆开派发**，防跨图印象复制。

---

# 九、校验与排障入口

- **离线格式闸门 & 在线 3 态字典校验**：断言清单、SQLite 缓存、`tag_aliases.json` 用法见 [normalization.md](../assets/normalization.md)，执行入口为 [validate_captions.py](../scripts/validate_captions.py)。
- **异常排查与源码对照**：截断、TE 缓存冲突、风格-内容纠缠与外部文献见 [troubleshooting.md](./troubleshooting.md) 与 [workflow.md](../assets/workflow.md)。

---

# 十、附录：模板、合规范例与速查

## 10.1 通用七槽位模板

```text
@触发词, [1主体] 1girl, solo, <角色名>,
[2外观着装] <发色> hair, <发型>, <瞳色> eyes, <表情>, <视线>, <身材>, <服装>, <饰品>, <身体特征>,
[3姿势动作] <姿势>, <手部动作>, <性描写>, <生殖器>, <体液>,
[4场景环境] <地点>, <道具>, <背景>,
[5光影色调] <day/night>, <sunlight>, <warm tone/cool tone>,
[6构图镜头] <upper body/cowboy shot/full body>, <from above/below>, <close-up>,
[7水印区] watermark, <颜色词>, signature, logo
```

## 10.2 合规范例（均满足 20~45 词且严格按槽位排序）

**画师风格示例（37 词，不含 `@myartist`）**：

```text
@myartist, 1girl, 1boy, hetero, solo focus, faceless male, blonde hair, long hair, straight hair, blue eyes, half-closed eyes, blush, open mouth, small breasts, skinny, pale skin, white shirt, pleated skirt, no panties, breasts out, pointy ears, nipples, sweat, wet, skirt lift, girl on top, penis, pussy, vaginal, cum, pussy juice, bedroom, window, day, soft lighting, warm tone, cowboy shot, from side
```

**角色 LoRA 简洁示例（22 词，不含 `@mychar`）**：

```text
@mychar, 1girl, solo, purple hair, very long hair, double bun, blunt bangs, red eyes, blush, looking at viewer, black shirt, white dress, frills, cleavage cutout, white thighhighs, hair ornament, tassel earrings, forehead jewel, brooch, skirt lift, pussy, white background, cowboy shot
```

## 10.3 常用阈值速查

| 项 | 值 | 备注 |
|---|---|---|
| 标签数目标区间 | **20~45** | **严格不含首位 `@触发词`**；超 45 可按 `trim_tiers` 裁剪 |
| 角色 tag 保留阈值 | ≥4 次 | 画风集默认保留并交用户裁决 |
| 固有特征剪切阈值 | ≥0.9 | 仅角色 LoRA 进阶使用，见 [target_types.md](./target_types.md) |
| 在线校验批量 | 120 / 批 | 必带 `limit=len*2`，区分 `VALID`/`INVALID`/`UNVERIFIED` |
