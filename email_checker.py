import imaplib
import email
import re
import time
import logging

logger = logging.getLogger(__name__)


class EmailChecker:
    def __init__(self, gmail_email, app_password):
        self.gmail_email = gmail_email
        self.app_password = app_password
        self.imap = None

    def connect(self):
        try:
            if self.imap is not None:
                try:
                    self.imap.logout()
                except Exception:
                    pass
                self.imap = None
            self.imap = imaplib.IMAP4_SSL("imap.gmail.com")
            self.imap.login(self.gmail_email, self.app_password)
            logger.info("[EMAIL] Подключён к Gmail")
            return True
        except Exception as e:
            logger.error(f"[EMAIL] Ошибка подключения: {e}")
            self.imap = None
            return False

    def disconnect(self):
        if self.imap:
            try:
                self.imap.logout()
            except Exception:
                pass
            self.imap = None

    def test_connection(self):
        if self.connect():
            try:
                self.imap.select("inbox")
                _, count = self.imap.search(None, "ALL")
                total = len(count[0].split()) if count[0] else 0
                return True, total
            except Exception as e:
                logger.error(f"[EMAIL] Ошибка test_connection: {e}")
                return False, 0
            finally:
                self.disconnect()
        return False, 0

    def find_roblox_code(self, target_email, timeout=120, check_interval=5):
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                if self.imap is None:
                    if not self.connect():
                        time.sleep(check_interval)
                        continue

                self.imap.select("inbox")
                search_query = f'(TO "{target_email}" FROM "noreply@roblox.com")'
                _, messages = self.imap.search(None, search_query)

                if messages[0]:
                    msg_ids = messages[0].split()
                    for latest_id in reversed(msg_ids[-5:]):
                        try:
                            _, msg_data = self.imap.fetch(latest_id, "(RFC822)")
                            raw_email = msg_data[0][1]
                            msg = email.message_from_bytes(raw_email)
                            body = self._get_body(msg)
                            code_match = re.search(r'\b(\d{6})\b', body)
                            if code_match:
                                code = code_match.group(1)
                                logger.info(f"[EMAIL] Найден код {code}")
                                return code
                        except Exception as e:
                            logger.warning(f"[EMAIL] Ошибка чтения письма: {e}")
                            continue

                time.sleep(check_interval)
            except Exception as e:
                logger.error(f"[EMAIL] Ошибка в цикле: {e}")
                self.disconnect()
                time.sleep(check_interval)

        logger.warning(f"[EMAIL] Код не найден за {timeout} сек")
        return None

    def _get_body(self, msg):
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type in ("text/plain", "text/html"):
                    try:
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode("utf-8", errors="ignore")
                            break
                    except Exception:
                        pass
        else:
            try:
                payload = msg.get_payload(decode=True)
                if payload:
                    body = payload.decode("utf-8", errors="ignore")
            except Exception:
                pass
        return body
