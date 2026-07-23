"""watercrawl.drivers — interaction DRIVERS that reveal MORE events than a single render shows (pagination/expansion).

用一句话讲完: 一个 IR 归档页常常只默认显示一部分事件,要点"年份下拉/年份 tab 条/load-more 按钮"或无限滚动才把整年
的事件调出来 —— 每种"翻页手段"各自一个文件:`years`(注入 JS 驱动 <select> 的多年视图)、`clicks`(点同一控件 N 次累积)、
`load_more`(自动走 load-more/无限滚动到穷尽)、`year_select`(从 DOM 自动发现年份 <select> 并逐年走)、`year_bar`(点击式
年份 tab/button 条,非 <select>)。WHY 独立子目录: crawl4ai 把 BFS/DFS/best-first 深度爬取算法拆进 `deep_crawling/` 各成
一个 strategy —— 我们照抄,翻页驱动一种一个文件,新增一种翻页模式不用碰渲染核心。{RESEARCH crawl4ai `deep_crawling/`}
[CONFIDENCE: CONFIRMED — verbatim 迁移自 pool.py 的 drive_* 函数].

每个 driver 都是 SELF-SKIPPING 的:页面没有它对应的控件(没年份 select / 没 load-more 按钮 / <2 个年份 chip)就返回
("", []),所以 caller 可以对任意 hub 无脑挨个调用,不匹配的自动跳过。
"""
