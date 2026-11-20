import os
import sys
import time
import json
import logging
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional

import requests
from selenium.webdriver.common.by import By
from seleniumbase import SB
from seleniumbase.common.exceptions import TimeoutException

# ====================== 配置 ======================
LOGIN_URL = "https://betadash.lunes.host/login?next=/"

WORKSPACE = os.environ.get("GITHUB_WORKSPACE", str(Path.cwd()))
OUTPUT_DIR = Path(WORKSPACE) / "output/screenshots"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("lunes_login")

for _noisy in ("seleniumbase", "selenium", "urllib3", "undetected_chromedriver"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


# ====================== 工具函数 ======================
def mask_email(email: str) -> str:
    if "@" not in email:
        return email[:1] + "***"
    local, domain = email.split("@", 1)
    masked_local = local[:1] + "***" if local else "***"
    if "." in domain:
        parts = domain.split(".")
        tld = parts[-1]
        masked_domain = f"{domain[0]}***.{tld}"
    else:
        masked_domain = domain[:1] + "***"
    return f"{masked_local}@{masked_domain}"


def log(msg: str, level: str = "INFO"):
    prefix = {"INFO": "[INFO]", "WARN": "[WARN]", "ERROR": "[ERROR]"}.get(level, "[INFO]")
    logger.info(f"{prefix} {msg}")


def send_tg_photo(token: str, chat_id: str, photo_path: str, caption: str):
    if not token or not chat_id:
        return
    if not photo_path or not os.path.exists(photo_path):
        log(f"截图文件不存在: {photo_path}", "WARN")
        return
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    try:
        with open(photo_path, "rb") as f:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
                timeout=30
            )
        resp.raise_for_status()
        log("Telegram 图片通知发送成功")
    except Exception as e:
        log(f"Telegram 通知异常: {e}", "ERROR")


def send_tg_message(token: str, chat_id: str, message: str):
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "parse_mode": "HTML"},
            timeout=15
        )
        resp.raise_for_status()
        log("Telegram 文本通知发送成功")
    except Exception as e:
        log(f"Telegram 文本通知异常: {e}", "ERROR")


def take_screenshot(sb, suffix: str) -> str:
    timestamp = datetime.now().strftime("%H%M%S")
    filename = f"lunes-{suffix}-{timestamp}.png"
    filepath = str(OUTPUT_DIR / filename)
    try:
        sb.save_screenshot(filepath)
        log(f"📸 截图保存: {filepath}")
        return filepath
    except Exception as e:
        log(f"截图失败: {e}", "WARN")
        return ""


# ====================== Turnstile 处理 ======================
def check_turnstile_solved(sb) -> bool:
    try:
        return bool(sb.execute_script('''
            var inp = document.querySelector('input[name="cf-turnstile-response"]');
            if (inp && inp.value && inp.value.length > 20) return true;
            var iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
            if (iframe && iframe.getAttribute("data-state") === "solved") return true;
            var success = document.getElementById('success');
            return !!(success && getComputedStyle(success).display !== 'none');
        '''))
    except Exception:
        return False


def wait_for_turnstile_success(sb, timeout: int = 40) -> bool:
    log("等待 Turnstile 验证...")
    start = time.time()
    last_click = 0
    last_log = 0
    while time.time() - start < timeout:
        if check_turnstile_solved(sb):
            log("✅ Turnstile 验证成功")
            return True
        now = time.time()
        if now - last_click > 3:
            try:
                sb.uc_gui_click_captcha()
                last_click = now
                if now - last_log > 10:
                    log("尝试点击 Turnstile")
                    last_log = now
            except Exception:
                pass
        time.sleep(1)
    log("⏰ Turnstile 验证超时", "WARN")
    return False


# ====================== 登录流程 ======================
def login(sb, email: str, password: str) -> bool:
    log(f"访问登录页: {LOGIN_URL}")
    sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=12)
    time.sleep(4)

    # 等待登录表单
    try:
        sb.wait_for_element_visible('input#email', timeout=15)
        log("✅ 找到登录表单")
    except TimeoutException:
        log("未找到登录表单，尝试重新连接...", "WARN")
        sb.uc_open_with_reconnect(LOGIN_URL, reconnect_time=12)
        time.sleep(5)
        try:
            sb.wait_for_element_visible('input#email', timeout=10)
        except TimeoutException:
            log("仍然未找到登录表单", "ERROR")
            return False

    # 填写表单
    try:
        sb.type('input#email', email)
        time.sleep(0.6)
        sb.type('input#password', password)
        time.sleep(0.6)
        log(f"✓ 已填写账号: {mask_email(email)}")
    except Exception as e:
        log(f"填写表单失败: {e}", "ERROR")
        return False

    # Turnstile
    if not wait_for_turnstile_success(sb, timeout=40):
        log("登录 Turnstile 未通过", "ERROR")
        return False

    # 提交
    log("提交登录...")
    try:
        # 优先使用原脚本的选择器
        if sb.is_element_visible("css:.submit-btn"):
            sb.click("css:.submit-btn")
        else:
            sb.click('button[type="submit"]')
    except Exception:
        try:
            sb.execute_script('document.querySelector("button[type=submit], .submit-btn").click()')
        except Exception as e:
            log(f"点击登录按钮失败: {e}", "ERROR")
            return False

    # 等待跳转
    log("等待登录结果...")
    for _ in range(18):
        current = sb.get_current_url()
        if "login" not in current and current != LOGIN_URL:
            break
        # 检查是否出现 Dashboard / Logout 等成功标识
        try:
            if sb.is_element_visible("text=Dashboard") or \
               sb.is_element_visible("text=Logout") or \
               sb.is_element_visible("text=Account Settings"):
                break
        except Exception:
            pass
        time.sleep(1)
    else:
        # 最后再检查一次页面内容
        page_source = sb.get_page_source().lower()
        if any(kw in page_source for kw in ["invalid", "incorrect", "wrong password", "failed"]):
            log("检测到登录错误关键词", "ERROR")
            return False
        log("登录后未成功跳转，可能失败", "ERROR")
        return False

    # 最终确认
    current = sb.get_current_url()
    if "login" in current:
        log("仍停留在登录页，登录失败", "ERROR")
        return False

    log("✅ 登录成功")
    log(f"当前 URL: {current}")
    return True


# ====================== 主流程 ======================
def process_login(email: str, password: str, tg_token: str, tg_chat: str):
    log("=" * 50)
    log(f"开始登录 Lunes Host | {mask_email(email)}")
    log("=" * 50)

    user_data_dir = tempfile.mkdtemp(prefix="lunes_usr_")
    with SB(
        uc=True,
        test=True,
        locale="en",
        headed=True,          # 强制可视模式（已去除无头）
        user_data_dir=user_data_dir,
        chromium_arg="--disable-blink-features=AutomationControlled"
    ) as sb:
        try:
            success = login(sb, email, password)
            screenshot = take_screenshot(sb, "success" if success else "fail")

            if success:
                caption = (
                    f"🎉 <b>Lunes Host 登录成功</b>\n\n"
                    f"📧 账户: <code>{email}</code>\n"
                    f"🌐 URL: {sb.get_current_url()}\n"
                    f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                )
                send_tg_photo(tg_token, tg_chat, screenshot, caption)
                # 也发一条纯文本（方便没有图片的情况）
                send_tg_message(tg_token, tg_chat, caption)
                return True
            else:
                caption = (
                    f"❌ <b>Lunes Host 登录失败</b>\n\n"
                    f"📧 账户: <code>{email}</code>\n"
                    f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                    f"请检查凭据或手动登录。"
                )
                send_tg_photo(tg_token, tg_chat, screenshot, caption)
                send_tg_message(tg_token, tg_chat, caption)
                return False

        except Exception as e:
            log(f"登录过程异常: {e}", "ERROR")
            screenshot = take_screenshot(sb, "exception")
            caption = (
                f"❌ <b>Lunes Host 脚本异常</b>\n\n"
                f"📧 账户: <code>{email}</code>\n"
                f"信息: {str(e)[:200]}\n"
                f"⏰ 时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            )
            send_tg_photo(tg_token, tg_chat, screenshot, caption)
            return False


def main():
    # ========== 从环境变量读取配置 ==========
    email = os.environ.get("LUNES_EMAIL", "").strip()
    password = os.environ.get("LUNES_PASSWORD", "").strip()
    tg_token = os.environ.get("TG_BOT_TOKEN", "").strip()
    tg_chat = os.environ.get("TG_CHAT_ID", "").strip()

    if not email or not password:
        log("缺少 LUNES_EMAIL 或 LUNES_PASSWORD 环境变量", "ERROR")
        print("\n请设置环境变量后运行，例如：")
        print('  export LUNES_EMAIL="your@email.com"')
        print('  export LUNES_PASSWORD="yourpassword"')
        print('  export TG_BOT_TOKEN="your_bot_token"   # 可选')
        print('  export TG_CHAT_ID="your_chat_id"       # 可选')
        print("  python lun.py")
        sys.exit(1)

    if not tg_token or not tg_chat:
        log("未配置 TG_BOT_TOKEN / TG_CHAT_ID，将跳过 Telegram 通知", "WARN")

    print("\n" + "=" * 50)
    print("  Lunes Host Auto Login Tool  v4.1")
    print("  SeleniumBase + Undetected Chrome")
    print("=" * 50)
    print("模式: 可视模式 👁️  (已去除无头)")
    print(f"通知: {'已启用 📱' if tg_token and tg_chat else '未启用'}")
    print(f"账号: {mask_email(email)}")
    print("=" * 50 + "\n")

    success = process_login(email, password, tg_token, tg_chat)

    print("\n" + "=" * 50)
    if success:
        print("✅ 登录成功！")
    else:
        print("❌ 登录失败")
    print("=" * 50 + "\n")

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
