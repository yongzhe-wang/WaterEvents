-- event_documents.via — WHICH EXTRACTOR PRODUCED THIS DOCUMENT.
--
-- 用一句话讲完: 同一个 kind='pdf' 的文档现在可能出自两条完全不同的路径 —— 坐标重建(读 PDF 文字层)或 docling
-- (渲染成像素让视觉模型猜网格)—— 而两条路的失败形态截然不同,库里却看不出任何一篇是哪条产的。
--
-- WHY 这一列不可省: 两条路径的质量差异是**按文档类型分的**,不是一条整体更好:
--   {2026-08-08 ExxonMobil 3Q24 IR Data Summary 逐格比对 —— docling 把 'Net income attributable to ExxonMobil' 这
--    一行填成了每股收益 1.92,正确值是 8,610;坐标路径 44 个数值行全对}
--   {2026-08-09 Ormat Q4-25 演示稿 —— 坐标路径 10 张表里 4 张是幻灯片版式噪声,而 docling 在同一份上是干净的}
-- 出了问题要能归因到路径,否则只能整体回滚而不是修那一条。
--
-- 现在只能靠**反推**: {2026-08-09 event_documents 非 html 共 4,512 篇,而 docling 服务累计调用 234 次}
-- → 95% 绕过了 docling。这个数字是从服务计数器减出来的,不是查出来的,而且服务一重启计数就归零。
-- [CONFIDENCE: CONFIRMED 100% — 两个数分别取自生产库和 docling 的 /health。]
--
-- 取值来自两条路径各自已有的字段,不新造词汇:
--   office: DocResult.via  → 'coords' | 'docling:text' | 'docling:ocr' | 'pypdf-fallback' | 'xlrd-fallback' | 'none'
--   html:   extract_html 的 tier → 'trafilatura' | 'readability' | 'resiliparse' | 'empty'
-- 可空: 这一列上线前写入的 41,000+ 行没有这个信息,填一个编造的值比留空更糟。
--
-- 上游触发: db_media.mark_enriched_media。下游连接: /api/today-media 的文档卡、以及任何按路径归因的排查。
alter table waterevents.event_documents add column if not exists via text;

-- 按路径统计是这一列唯一的查询形态,而 kind 几乎总是一起出现(「pdf 里有多少走了坐标」)。
create index if not exists event_documents_via_idx on waterevents.event_documents (via, kind);
