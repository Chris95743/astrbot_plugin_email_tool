from __future__ import annotations

import asyncio
import os
import platform
import json
import re
import smtplib
import socket
import ssl
import time
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr
from typing import Iterable, List, Optional

import astrbot.api.star as star
from astrbot.api import llm_tool, logger
from astrbot.api.all import regex
from astrbot.api.event import AstrMessageEvent

try:  # 可选依赖：用于系统资源监控
	import psutil  # type: ignore
except Exception:  # pragma: no cover - 在未安装 psutil 时降级
	psutil = None  # type: ignore

try:  # 可选依赖：HTTP 请求（异步）
	import httpx  # type: ignore
except Exception:
	httpx = None  # type: ignore


class Main(star.Star):

	def __init__(self, context: star.Context, config: Optional[dict] = None) -> None:
		self.context = context
		self.config = config or {}
		# 发送节流：记录上次成功发送的时间戳（秒）
		self._last_sent_ts: float = 0.0
		# 告警控制
		self._alert_task: Optional[asyncio.Task] = None
		self._last_alert_ts: float = 0.0
		# Napcat 检测
		self._napcat_task: Optional[asyncio.Task] = None
		self._napcat_last_status: Optional[bool] = None  # True=online, False=offline, None=未知
		self._napcat_last_alert_ts: float = 0.0
		self._napcat_last_checked_ts: float = 0.0
		self._napcat_fail_count: int = 0

	async def initialize(self):
		# 默认启用函数工具
		self.context.activate_llm_tool("smtp_send_html_email")
		logger.info("[email_tool] 函数工具 smtp_send_html_email 已启用")

		# 启动服务器状态报警任务（可选）
		if bool(self.config.get("enable_server_alerts", False)):
			if psutil is None:
				logger.warning("[email_tool] 已启用服务器报警，但未安装 psutil，监控功能将不可用。请在插件 requirements.txt 中安装 psutil。")
			else:
				if not self._normalize_addresses(self.config.get("alert_recipients")):
					logger.warning("[email_tool] 启用了服务器报警，但未配置 alert_recipients，已跳过启动监控任务。")
				else:
					self._alert_task = asyncio.create_task(self._alert_loop())
					logger.info("[email_tool] 服务器内存监控任务已启动")

		# 启动 Napcat 掉线检测（可选）
		if bool(self.config.get("enable_napcat_monitor", False)):
			if httpx is None:
				logger.warning("[email_tool] 已启用 Napcat 监控，但未安装 httpx，监控不可用。请在插件 requirements.txt 中安装 httpx。")
			else:
				if not (self.config.get("napcat_base_url") and (self.config.get("napcat_token") or self.config.get("napcat_credential"))):
					logger.warning("[email_tool] Napcat 监控缺少 base_url 或 token/credential，已跳过启动。")
				else:
					if not self._normalize_addresses(self.config.get("napcat_alert_recipients")):
						logger.warning("[email_tool] 已启用 Napcat 监控，但未配置 napcat_alert_recipients。")
					self._napcat_task = asyncio.create_task(self._napcat_loop())
					logger.info("[email_tool] Napcat 掉线监控任务已启动")

	async def terminate(self):
		# 停止监控任务
		if self._alert_task and not self._alert_task.done():
			self._alert_task.cancel()
			try:
				await self._alert_task
			except asyncio.CancelledError:
				pass
			logger.info("[email_tool] 服务器内存监控任务已停止")

		# 停止 Napcat 任务
		if self._napcat_task and not self._napcat_task.done():
			self._napcat_task.cancel()
			try:
				await self._napcat_task
			except asyncio.CancelledError:
				pass
			logger.info("[email_tool] Napcat 掉线监控任务已停止")

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
		# 注意：仅用于收件人聚合，发送时 SMTP 会在投递时去除 Bcc 头部（不会泄露给收件人）
		if bcc_list:
			msg["Bcc"] = ", ".join(bcc_list)

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

	# ------------------------- 内部：报警与渲染 -------------------------
	async def _alert_loop(self):
		"""后台循环检查内存占用，并在超过阈值时发送告警邮件（带冷却）。"""
		check_interval = int(self.config.get("check_interval_seconds", 30) or 30)
		while True:
			try:
				await self._check_and_alert()
			except asyncio.CancelledError:
				raise
			except Exception as e:
				logger.error(f"[email_tool] 监控任务异常：{e}", exc_info=True)
			finally:
				await asyncio.sleep(max(5, check_interval))

	async def _check_and_alert(self):
		if psutil is None:
			return
		threshold = int(self.config.get("mem_threshold_percent", 80) or 80)
		cooldown_min = int(self.config.get("alert_cooldown_minutes", 30) or 30)
		now_ts = time.time()
		if cooldown_min > 0 and self._last_alert_ts and (now_ts - self._last_alert_ts) < cooldown_min * 60:
			return

		vmem = psutil.virtual_memory()
		mem_percent = float(vmem.percent)
		if mem_percent < threshold:
			return

		# 汇总服务器信息
		server_name = socket.gethostname()
		os_version = f"{platform.system()} {platform.release()} ({platform.version()})"
		boot_ts = getattr(psutil, "boot_time", lambda: None)() if psutil else None
		uptime_s = int(now_ts - boot_ts) if boot_ts else 0
		uptime_h = uptime_s // 3600
		uptime_m = (uptime_s % 3600) // 60
		cpu_percent = 0.0
		try:
			# 快速获取 CPU 百分比（不阻塞长时间）
			cpu_percent = float(psutil.cpu_percent(interval=None))
		except Exception:
			pass
		mem_total_gb = round(vmem.total / (1024**3), 2)
		mem_used_gb = round((vmem.total - vmem.available) / (1024**3), 2)
		now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

		data = {
			"server_name": server_name,
			"os_version": os_version,
			"now": now_str,
			"mem_total_gb": mem_total_gb,
			"mem_used_gb": mem_used_gb,
			"mem_percent": mem_percent,
			"cpu_percent": cpu_percent,
			"uptime_h": uptime_h,
			"uptime_m": uptime_m,
		}

		subject = f"[告警] 服务器内存使用率 {mem_percent:.0f}% 超过阈值 {threshold}%"
		html_body = self._render_alert_template("alert_memory.html", data)
		to_list = self._normalize_addresses(self.config.get("alert_recipients"))
		if not to_list:
			logger.warning("[email_tool] 触发内存告警，但未配置 alert_recipients，已跳过发送。")
			return
		# 发送（忽略常规发送间隔，使用独立冷却）
		resp = await self._send_html_via_config(subject, html_body, to_list, respect_interval=False)
		logger.info(f"[email_tool] 内存告警发送结果：{resp}")
		self._last_alert_ts = now_ts

	# ------------------------- 内部：Napcat 监控 -------------------------
	async def _napcat_login(self, base_url: str, token: str, allow_insecure: bool) -> Optional[str]:
		"""调用 Napcat /api/auth/login 使用 token+hash 换取 Credential（Base64）。(异步 httpx)"""
		if httpx is None:
			return None
		try:
			import hashlib
			hash_val = hashlib.sha256((token + ".napcat").encode("utf-8")).hexdigest()
			url = base_url.rstrip("/") + "/api/auth/login"
			timeout = httpx.Timeout(5.0)
			async with httpx.AsyncClient(verify=not allow_insecure, timeout=timeout) as client:
				resp = await client.post(url, json={"token": token, "hash": hash_val})
				resp.raise_for_status()
				data = resp.json()
				# 兼容不同的返回结构，尝试从多个 key 取 Credential
				if isinstance(data, dict):
					# 常见：{"code":0,"data":"Base64Cred..."}
					if isinstance(data.get("data"), str):
						return data.get("data")
					# 或 {"data":{"credential":"..."}} / {"data":{"Credential":"..."}}
					inner = data.get("data")
					if isinstance(inner, dict):
						for key in ("credential", "Credential", "CREDENTIAL"):
							val = inner.get(key)
							if isinstance(val, str):
								return val
					# 或 顶层 {"credential":"..."} / {"Credential":"..."}
					for key in ("credential", "Credential", "CREDENTIAL"):
						val = data.get(key)
						if isinstance(val, str):
							return val
				logger.error(f"[email_tool] Napcat 登录响应无法解析：{data}")
				return None
		except Exception as e:
			logger.error(f"[email_tool] Napcat 登录失败：{e}")
			return None

	async def _napcat_get_login_info(self, base_url: str, credential: str, allow_insecure: bool) -> Optional[dict]:
		"""调用 Napcat /api/QQLogin/GetQQLoginInfo 返回登录状态。(异步 httpx)"""
		if httpx is None:
			return None
		try:
			url = base_url.rstrip("/") + "/api/QQLogin/GetQQLoginInfo"
			headers = {"Authorization": f"Bearer {credential}", "Content-Type": "application/json"}
			timeout = httpx.Timeout(5.0)
			async with httpx.AsyncClient(verify=not allow_insecure, timeout=timeout) as client:
				resp = await client.post(url, headers=headers, json={})
				resp.raise_for_status()
				return resp.json()
		except Exception as e:
			logger.error(f"[email_tool] Napcat 获取登录信息失败：{e}")
			return None

	async def _napcat_loop(self):
		interval = int(self.config.get("napcat_interval_seconds", 60) or 60)
		cooldown_min = int(self.config.get("napcat_alert_cooldown_minutes", 30) or 30)
		allow_insecure = bool(self.config.get("napcat_allow_insecure", False))
		base_url = (self.config.get("napcat_base_url") or "").strip()
		uin = (self.config.get("napcat_uin") or "").strip()
		credential_cfg = (self.config.get("napcat_credential") or "").strip()
		token = (self.config.get("napcat_token") or "").strip()
		fail_threshold = int(self.config.get("napcat_failure_threshold", 2) or 2)
		credential_cache: Optional[str] = credential_cfg or None
		while True:
			try:
				if not credential_cache and token:
					credential_cache = await self._napcat_login(base_url, token, allow_insecure)
					if not credential_cache:
						# 登录失败计入失败次数
						self._napcat_fail_count += 1
						if self._napcat_fail_count >= fail_threshold:
							# 达到失败阈值，判定为离线
							now_ts = time.time()
							self._napcat_last_checked_ts = now_ts
							if (self._napcat_last_status is True):
								if not (cooldown_min > 0 and self._napcat_last_alert_ts and (now_ts - self._napcat_last_alert_ts) < cooldown_min * 60):
									await self._napcat_send_offline_alert(base_url, uin, False)
									self._napcat_last_alert_ts = now_ts
							self._napcat_last_status = False
							logger.warning("[email_tool] Napcat 登录连续失败，暂时判定为离线。")
						await asyncio.sleep(max(10, interval))
						continue
				info = await self._napcat_get_login_info(base_url, credential_cache, allow_insecure)
				if not info:
					# 可能 Credential 过期或连接失败
					self._napcat_fail_count += 1
					if self._napcat_fail_count >= fail_threshold:
						# 达到失败阈值，判定为离线
						now_ts = time.time()
						self._napcat_last_checked_ts = now_ts
						if (self._napcat_last_status is True):
							if not (cooldown_min > 0 and self._napcat_last_alert_ts and (now_ts - self._napcat_last_alert_ts) < cooldown_min * 60):
								await self._napcat_send_offline_alert(base_url, uin, False)
								self._napcat_last_alert_ts = now_ts
						self._napcat_last_status = False
						logger.warning("[email_tool] Napcat 获取登录信息连续失败，暂时判定为离线。")
					# 清空凭据尝试重登
					credential_cache = None
					await asyncio.sleep(max(10, interval))
					continue
				# 解析 online 字段
				online = None
				try:
					if isinstance(info, dict):
						if isinstance(info.get("data"), dict) and "online" in info["data"]:
							online = bool(info["data"]["online"])  # noqa: E712
						elif "online" in info:
							online = bool(info["online"])  # noqa: E712
				except Exception:
					pass
				if online is None:
					logger.error(f"[email_tool] Napcat 登录信息无法解析：{json.dumps(info, ensure_ascii=False)}")
					self._napcat_fail_count += 1
					if self._napcat_fail_count >= fail_threshold:
						now_ts = time.time()
						self._napcat_last_checked_ts = now_ts
						if (self._napcat_last_status is True):
							if not (cooldown_min > 0 and self._napcat_last_alert_ts and (now_ts - self._napcat_last_alert_ts) < cooldown_min * 60):
								await self._napcat_send_offline_alert(base_url, uin, False)
								self._napcat_last_alert_ts = now_ts
						self._napcat_last_status = False
						logger.warning("[email_tool] Napcat 数据解析失败次数达阈值，暂时判定为离线。")
					await asyncio.sleep(max(10, interval))
					continue

				# 状态变更：Online -> Offline 触发警报；冷却控制
				now_ts = time.time()
				self._napcat_last_checked_ts = now_ts
				# 成功获取状态，失败计数清零
				self._napcat_fail_count = 0
				if (self._napcat_last_status is True) and (online is False):
					if cooldown_min > 0 and self._napcat_last_alert_ts and (now_ts - self._napcat_last_alert_ts) < cooldown_min * 60:
						pass
					else:
						await self._napcat_send_offline_alert(base_url, uin, online)
						self._napcat_last_alert_ts = now_ts
				# 更新上次状态
				self._napcat_last_status = online
			except asyncio.CancelledError:
				raise
			except Exception as e:
				logger.error(f"[email_tool] Napcat 监控异常：{e}", exc_info=True)
			finally:
				await asyncio.sleep(max(5, interval))

	async def _napcat_send_offline_alert(self, base_url: str, uin: str, online: bool):
		to_list = self._normalize_addresses(self.config.get("napcat_alert_recipients")) or self._normalize_addresses(self.config.get("alert_recipients"))
		if not to_list:
			logger.warning("[email_tool] 触发 Napcat 掉线告警，但未配置收件人（napcat_alert_recipients 或 alert_recipients）。已跳过发送。")
			return
		status_text = "离线" if not online else "在线"
		subject = f"[告警] Napcat 掉线：当前状态 {status_text}"
		data = {
			"now": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
			"base_url": base_url,
			"uin": uin or "-",
			"status": status_text,
		}
		html = self._render_alert_template("alert_napcat_offline.html", data)
		resp = await self._send_html_via_config(subject, html, to_list, respect_interval=False)
		logger.info(f"[email_tool] Napcat 掉线邮件发送结果：{resp}")

	# ------------------------- 指令：/猫猫查询 -------------------------
	@regex(r"^\s*/?(猫猫查询|napcat状态|猫猫|猫猫状态)\s*$", desc="查询 Napcat 当前状态")
	async def cmd_query_napcat(self, event: AstrMessageEvent):
		base_url = (self.config.get("napcat_base_url") or "").strip()
		allow_insecure = bool(self.config.get("napcat_allow_insecure", False))
		uin = (self.config.get("napcat_uin") or "").strip()
		token = (self.config.get("napcat_token") or "").strip()
		credential_cfg = (self.config.get("napcat_credential") or "").strip()
		if not base_url:
			text = "Napcat 未配置：请在插件配置中设置 napcat_base_url。"
			event.set_result(event.make_result().message(text).stop_event())
			return
		if httpx is None:
			text = "Napcat 查询不可用：未安装 httpx 依赖。"
			event.set_result(event.make_result().message(text).stop_event())
			return

		# 优先使用运行中缓存状态；如果未知则尝试一次即时查询
		status_text = None
		if self._napcat_last_status is not None:
			status_text = "在线" if self._napcat_last_status else "离线"
			last_time = datetime.fromtimestamp(self._napcat_last_checked_ts).strftime("%Y-%m-%d %H:%M:%S") if self._napcat_last_checked_ts else "-"
			text = (
				f"Napcat 当前状态：{status_text}\n"
				f"最近检查：{last_time}\n"
				f"地址：{base_url}\n"
				f"QQ：{uin or '-'}"
			)
			# 直接返回缓存状态
			event.set_result(event.make_result().message(text).stop_event())
			return

		# 进行一次即时检查
		credential = credential_cfg
		if not credential and token:
			credential = await self._napcat_login(base_url, token, allow_insecure)
		if not credential:
			text = "Napcat 查询失败：无法获取 Credential（请检查 token 或配置）。"
			event.set_result(event.make_result().message(text).stop_event())
			return
		info = await self._napcat_get_login_info(base_url, credential, allow_insecure)
		if not info:
			text = "Napcat 查询失败：无法获取登录信息（接口错误或超时）。"
			event.set_result(event.make_result().message(text).stop_event())
			return
		online = None
		try:
			if isinstance(info, dict):
				if isinstance(info.get("data"), dict) and "online" in info["data"]:
					online = bool(info["data"]["online"])  # noqa: E712
				elif "online" in info:
					online = bool(info["online"])  # noqa: E712
		except Exception:
			pass
		status_text = "在线" if online else ("离线" if online is False else "未知")
		text = (
			f"Napcat 当前状态：{status_text}\n"
			f"地址：{base_url}\n"
			f"QQ：{uin or '-'}"
		)
		event.set_result(event.make_result().message(text).stop_event())

	def _render_alert_template(self, filename: str, data: dict) -> str:
		"""加载并渲染模板，若不存在则使用内置简易模板。"""
		dirname = os.path.dirname(__file__)
		tmpl_path = os.path.join(dirname, "templates", filename)
		if os.path.isfile(tmpl_path):
			try:
				with open(tmpl_path, "r", encoding="utf-8") as f:
					tmpl = f.read()
				return tmpl.format(**data)
			except Exception as e:
				logger.error(f"[email_tool] 渲染模板失败，使用内置模板：{e}")
		# 内置备用模板（避免 format 与 CSS 冲突，尽量少用花括号样式）
		fallback = (
			"<div style='font-family:Segoe UI,Arial;line-height:1.6;padding:16px'>"
			"<h2 style='color:#d4380d;margin:0 0 12px'>服务器内存告警</h2>"
			"<p>时间：{now}</p>"
			"<p>服务器：{server_name}</p>"
			"<p>系统：{os_version}</p>"
			"<p>CPU 使用率：{cpu_percent:.1f}%</p>"
			"<p>内存：{mem_used_gb} GB / {mem_total_gb} GB（{mem_percent:.0f}%）</p>"
			"<p>已运行：{uptime_h} 小时 {uptime_m} 分钟</p>"
			"<hr style='border:none;border-top:1px solid #eee;margin:16px 0'/>"
			"<p style='color:#888;font-size:12px'>本邮件由 AstrBot 自动发送</p>"
			"</div>"
		)
		return fallback.format(**data)

	async def _send_html_via_config(self, subject: str, html_body: str, to_list: List[str], respect_interval: bool = True) -> str:
		"""使用当前配置发送 HTML 邮件。可选择忽略常规发送频率限制。"""
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

		if respect_interval:
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

		# 白名单校验
		for addr in to_list:
			if not self._domain_allowed(addr):
				return f"目标地址域名不在白名单内：{addr}。请检查 allow_domains 配置。"

		try:
			msg = self._build_message(subject, html_body, from_address, from_name, to_list, [], [])
		except Exception as e:
			logger.error(f"[email_tool] 构建邮件失败: {e}", exc_info=True)
			return f"构建邮件失败：{e}"

		try:
			if dry_run:
				logger.info(
					f"[email_tool] Dry-run：模拟发送 -> to={to_list}, subject={subject}"
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
				detail = "; ".join([f"{k}: {v}" for k, v in refused.items()])
				self._last_sent_ts = time.time()
				return f"发送请求已提交，但以下收件人被SMTP拒收：{detail}"
			sent_total = len(to_list)
			self._last_sent_ts = time.time()
			return f"发送成功：共 {sent_total} 个收件人。"
		except Exception as e:
			logger.error("[email_tool] 发送失败", exc_info=True)
			return f"发送失败：{e}"

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
			to(string): 收件人邮箱，支持“逗号/分号/空格”分隔的多地址；也兼容数组输入
			subject(string): 邮件主题
			html_body(string): 邮件 HTML 正文，建议使用行内 CSS，兼容主流客户端，尽量使用精美的样式。
			cc(string): 抄送人，支持分隔字符串；也兼容数组输入（可选）
			bcc(string): 密送人，支持分隔字符串；也兼容数组输入（可选）
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
