"""审核网站实现（内置在插件里，也可独立部署）。

同一个实现有两种跑法：
1. **内置**：插件配置里打开 `web_site_enabled`，插件启动时会在后台线程里跑起这个站点，
   并自动把自己的验证码/规则同步过去（无需配置 url/token）；
2. **独立**：用仓库里的 `review-site/serve.py`（或站点 zip）单独跑一个进程，
   插件用 `web_review_url` + `web_review_token` 对接。

本模块只用 Python 标准库：`http.server` + `sqlite3` + `urllib`。
"""
