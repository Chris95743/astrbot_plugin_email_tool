from __future__ import annotations

import asyncio
import re
import smtplib
import ssl
import time
from email.message import EmailMessage
from email.utils import formataddr
from typing import Iterable, List, Optional

import astrbot.api.star as star
from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent


class Main(star.Star):

	def __init__(self, context: star.Context, config: Optional[dict] = None) -> None:
		self.context = context
		self.config = config or {}
		# 发送节流：记录上次成功发送的时间戳（秒）
		self._last_sent_ts: float = 0.0

	async def initialize(self):
		# 默认启用函数工具
		self.context.activate_llm_tool("smtp_send_html_email")
		logger.info("[email_tool] 函数工具 smtp_send_html_email 已启用")

	# ------------------------- 内部工具函数 -------------------------
	def _normalize_addresses(self, value: Optional[Iterable[str] | str]) -> List[str]:
		"""将输入收件人参数统一为邮箱字符串列表，并做基础清洗。"""
		if not value:
			return []
		if isinstance(value, str):
			# 支持逗号/分号/空白分隔
			parts = re.split(r"[;,\s]+", value.strip())
		else:
			parts = []
			for v in value:
				if not v:
					continue
				parts.extend(re.split(r"[;,\s]+", str(v).strip()))
		# 过滤空与重复，保持顺序
		seen = set()
		result = []
		for p in parts:
			if not p or p in seen:
				continue
			seen.add(p)
			result.append(p)
		return result

	def _domain_allowed(self, email_addr: str) -> bool:
		"""校验邮箱域名是否在白名单（若配置了的话）。"""
		allow_domains = self.config.get("allow_domains") or []
		if not allow_domains:
			return True
		try:
			domain = email_addr.split("@", 1)[1].lower()
		except Exception:
			return False
		return any(domain == d.lower() or domain.endswith("." + d.lower()) for d in allow_domains)

	def _build_message(
		self,
		subject: str,
		html_body: str,
		from_addr: str,
		from_name: Optional[str],
		to_list: List[str],
		cc_list: List[str],
		bcc_list: List[str],
	) -> EmailMessage:
		msg = EmailMessage()
		msg["Subject"] = subject
		msg["From"] = formataddr((from_name or "AstrBot", from_addr))
		if to_list:
			msg["To"] = ", ".join(to_list)
		if cc_list:
			msg["Cc"] = ", ".join(cc_list)

		# 纯文本降级内容 + HTML 正文
		msg.set_content("This is an HTML email. If you see this, your client is showing the plain-text fallback.")
		msg.add_alternative(html_body, subtype="html")
		return msg

	def _send_sync(
		self,
		msg: EmailMessage,
		smtp_host: str,
		smtp_port: int,
		username: Optional[str],
		password: Optional[str],
		use_ssl: bool,
		use_starttls: bool,
		debug: bool,
	) -> dict:
		"""在线程中执行的同步发送逻辑，避免阻塞事件循环。

		返回值为被拒收的收件人字典（与 smtplib.sendmail 一致）。为空字典表示全部接受。
		"""
		context = ssl.create_default_context()
		if use_ssl:
			with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
				server.set_debuglevel(1 if debug else 0)
				if username:
					server.login(username, password or "")
				# 返回被拒收的收件人 dict
				refused = server.send_message(msg)
				server.quit()
				return refused
		else:
			with smtplib.SMTP(smtp_host, smtp_port) as server:
				server.set_debuglevel(1 if debug else 0)
				server.ehlo()
				if use_starttls:
					server.starttls(context=context)
					server.ehlo()
				if username:
					server.login(username, password or "")
				refused = server.send_message(msg)
				server.quit()
				return refused

	# ------------------------- LLM 函数工具 -------------------------
	@llm_tool("smtp_send_html_email")
	async def smtp_send_html_email(
		self,
		event: AstrMessageEvent,
		to: list | str,
		subject: str,
		html_body: str,
		cc: list | str = None,
		bcc: list | str = None,
	) -> str:
		"""使用插件配置的 SMTP 服务发送一封 HTML 邮件。

		Args:
			to(array): 收件人邮箱，可为字符串（支持逗号/分号/空格分隔）或字符串数组
			subject(string): 邮件主题
			html_body(string): 邮件 HTML 正文，建议使用行内 CSS，兼容主流客户端，尽量使用精美的样式。
			cc(array): 抄送人，可为字符串或数组（可选）
			bcc(array): 密送人，可为字符串或数组（可选）
		"""
		# 读取配置
		smtp_host = (self.config.get("smtp_host") or "").strip()
		smtp_port = int(self.config.get("smtp_port") or 0)
		username = (self.config.get("username") or "").strip() or None
		password = (self.config.get("password") or "").strip() or None
		use_ssl = bool(self.config.get("use_ssl", True))
		use_starttls = bool(self.config.get("use_starttls", False))
		from_address = (self.config.get("from_address") or "").strip()
		from_name = (self.config.get("from_display_name") or "AstrBot").strip()
		dry_run = bool(self.config.get("dry_run", False))
		smtp_debug = bool(self.config.get("smtp_debug", False))

		# 发送间隔限制（节流）
		interval = int(self.config.get("send_interval_seconds", 60) or 60)
		now = time.time()
		if interval > 0 and self._last_sent_ts > 0 and (now - self._last_sent_ts) < interval:
			remain = int(interval - (now - self._last_sent_ts))
			return f"发送过于频繁，请 {remain} 秒后再试（最小间隔 {interval} 秒）。"

		# 基本校验
		if not smtp_host or not smtp_port:
			return "SMTP 配置不完整：请在插件配置中设置 smtp_host 与 smtp_port。"
		if not from_address or "@" not in from_address:
			return "发件人地址 from_address 未设置或无效。"
		if use_ssl and use_starttls:
			return "配置冲突：use_ssl 与 use_starttls 不能同时为真。"

		# 规范化收件人
		to_list = self._normalize_addresses(to)
		cc_list = self._normalize_addresses(cc)
		bcc_list = self._normalize_addresses(bcc)

		if not to_list:
			return "缺少收件人。请提供至少一个有效的收件人邮箱。"

		# 白名单校验（如配置）
		for addr in [*to_list, *cc_list, *bcc_list]:
			if not self._domain_allowed(addr):
				return f"目标地址域名不在白名单内：{addr}。请检查 allow_domains 配置。"

		try:
			msg = self._build_message(subject, html_body, from_address, from_name, to_list, cc_list, bcc_list)
		except Exception as e:
			logger.error(f"[email_tool] 构建邮件失败: {e}", exc_info=True)
			return f"构建邮件失败：{e}"

		# 实际发送
		try:
			if dry_run:
				logger.info(
					f"[email_tool] Dry-run：模拟发送 -> to={to_list}, cc={cc_list}, bcc={bcc_list}, subject={subject}"
				)
				return "Dry-run：已模拟发送（未实际投递）。"

			refused = await asyncio.to_thread(
				self._send_sync,
				msg,
				smtp_host,
				smtp_port,
				username,
				password,
				use_ssl,
				use_starttls,
				smtp_debug,
			)
			if refused:
				# 格式化拒收信息
				detail = "; ".join([f"{k}: {v}" for k, v in refused.items()])
				# 即便有拒收，也可能部分投递成功，视为一次有效发送用于节流
				self._last_sent_ts = now
				return f"发送请求已提交，但以下收件人被SMTP拒收：{detail}"
			sent_total = len(to_list) + len(cc_list) + len(bcc_list)
			self._last_sent_ts = now
			return f"发送成功：共 {sent_total} 个收件人（含抄送/密送）。"
		except Exception as e:
			logger.error("[email_tool] 发送失败", exc_info=True)
			return f"发送失败：{e}"

