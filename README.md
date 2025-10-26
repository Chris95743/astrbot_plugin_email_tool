astrbot_plugin_email_tool
=========================

让 LLM 自主调用 SMTP 工具发送精美 HTML 邮件。

功能
- 在对话中，当模型判断需要发送邮件时，可调用函数工具 `smtp_send_html_email`
- 支持收件人/抄送/密送数组或以逗号/分号/空白分隔的字符串
- 内置 HTML 正文投递，带纯文降级
- 支持 SSL 直连或 STARTTLS
- 可选域名白名单、Dry-run 调试
 - 可选 SMTP 调试日志（smtp_debug），排查拒收/鉴权问题
 - 发送间隔保护（send_interval_seconds，默认60秒）防止模型误触频繁发信

配置
在 AstrBot WebUI -> 插件 -> 本插件配置 中填写下列项（见 `_conf_schema.json`）：
- smtp_host, smtp_port
- use_ssl 或 use_starttls（二选一）
- username, password（如服务器需要认证）
- from_address, from_display_name
- allow_domains（可选）
- dry_run（可选）
 - smtp_debug（可选）
 - send_interval_seconds（可选，默认60秒）

LLM 工具
名称：`smtp_send_html_email`

参数：
- to(array): 收件人邮箱，可为字符串（支持逗号/分号/空格分隔）或数组
- subject(string): 邮件主题
- html_body(string): 邮件 HTML 正文
- cc(array): 抄送（可选）
- bcc(array): 密送（可选）

返回：
- 成功/失败的简要文本，用于辅助模型进行后续汇总与反馈。

排障建议
- 若提示“发送成功”但收件人未收到：
	1) 打开 smtp_debug 观察 SMTP 会话；
	2) 关注返回的“被拒收的收件人”列表；
	3) 确认邮箱已开启 SMTP/客户端授权码功能，并使用授权码作为密码；
	4) 确保 from_address 与登录账号一致（多数服务商强制要求）；
	5) 检查收件方垃圾箱/拦截；
	6) 先向同域/自发件地址发送自测，确认基础链路；
	7) 业务投递建议添加品牌签名、较完整正文，避免被反垃圾策略拦截。

注意
- 为了兼容多数邮件客户端，请尽量使用行内 CSS 的 HTML 模板。
- 建议使用授权码而非明文密码；如需额外安全控制，可结合域名白名单。 
