"""
Обёртка над FunPayAPI с патчами багов библиотеки
"""
import types as _types
import time as _time
from typing import Optional
import requests
from bs4 import BeautifulSoup
from FunPayAPI import Account, Runner
from FunPayAPI.common import exceptions as fp_exceptions
from FunPayAPI.common.enums import MessageTypes
from FunPayAPI import types as fp_types
from FunPayAPI.updater import runner as _runner_module
import config


def _patched_parse_messages(self, json_messages, chat_id,
                             interlocutor_id: Optional[int] = None,
                             interlocutor_username: Optional[str] = None,
                             from_id: int = 0):
    messages = []
    ids = {self.id: self.username, 0: "FunPay"}
    badges = {}
    if interlocutor_id is not None:
        ids[interlocutor_id] = interlocutor_username
    for i in json_messages:
        if i["id"] < from_id:
            continue
        author_id = i["author"]
        parser = BeautifulSoup(i["html"], "html.parser")
        if None in [ids.get(author_id), badges.get(author_id)] and \
                (author_div := parser.find("div", {"class": "media-user-name"})):
            if badges.get(author_id) is None:
                badge = author_div.find("span")
                badges[author_id] = badge.text if badge else 0
            if ids.get(author_id) is None:
                a_tag = author_div.find("a")
                author = a_tag.text.strip() if a_tag else None
                ids[author_id] = author
                if self.chat_id_private(chat_id) and author_id == interlocutor_id and not interlocutor_username:
                    interlocutor_username = author
                    ids[interlocutor_id] = interlocutor_username
        image_link = None
        message_text = None
        img_a = parser.find("a", {"class": "chat-img-link"})
        if self.chat_id_private(chat_id) and img_a:
            image_link = img_a.get("href")
        else:
            if author_id == 0:
                alert_div = parser.find("div", {"class": "alert alert-with-icon alert-info"})
                message_text = alert_div.text.strip() if alert_div else None
            else:
                text_div = parser.find("div", {"class": "chat-msg-text"}) \
                    or parser.find("div", {"class": "message-text"})
                message_text = text_div.text if text_div else None
        if message_text is None and image_link is None:
            fallback = parser.get_text(separator=" ", strip=True)
            message_text = fallback or "[нераспознанное сообщение]"
        by_bot = False
        if not image_link and message_text.startswith(self.bot_character):
            message_text = message_text.replace(self.bot_character, "", 1)
            by_bot = True
        message_obj = fp_types.Message(i["id"], message_text, chat_id, interlocutor_username,
                                        None, author_id, i["html"], image_link, determine_msg_type=False)
        message_obj.by_bot = by_bot
        message_obj.type = MessageTypes.NON_SYSTEM if author_id != 0 else message_obj.get_message_type()
        messages.append(message_obj)
    for i in messages:
        i.author = ids.get(i.author_id)
        i.chat_name = interlocutor_username
        i.badge = badges.get(i.author_id) if badges.get(i.author_id) != 0 else None
    return messages


Account._Account__parse_messages = _patched_parse_messages


def _patched_generate_new_message_events(self, chats_data):
    attempts = 3
    chats = None
    while attempts:
        attempts -= 1
        try:
            chats = self.account.get_chats_histories(chats_data)
            break
        except fp_exceptions.RequestFailedError as e:
            _runner_module.logger.error(e)
        except Exception:
            _runner_module.logger.error(f"Не удалось получить истории чатов {list(chats_data.keys())}.")
            _runner_module.logger.debug("TRACEBACK", exc_info=True)
            _time.sleep(1)
    else:
        _runner_module.logger.error(
            f"Не удалось получить истории чатов {list(chats_data.keys())}: превышено кол-во попыток."
        )
        return {}
    result = {}
    for cid in chats:
        messages = chats[cid]
        result[cid] = []
        self.by_bot_ids[cid] = self.by_bot_ids.get(cid) or []
        if self.last_messages_ids.get(cid):
            messages = [i for i in messages if i.id > self.last_messages_ids[cid]]
        if not messages:
            continue
        if self.by_bot_ids.get(cid):
            for i in messages:
                if not i.by_bot and i.id in self.by_bot_ids[cid]:
                    i.by_bot = True
        stack = _runner_module.MessageEventsStack()
        if not self.last_messages_ids.get(cid):
            if init_msg_text := self.init_messages.get(cid):
                del self.init_messages[cid]
                temp = []
                for i in reversed(messages):
                    if i.text[:250] == init_msg_text:
                        break
                    temp.append(i)
                messages = list(reversed(temp))
            else:
                messages = messages[-1:]
        if not messages:
            continue
        self.last_messages_ids[cid] = messages[-1].id
        self.by_bot_ids[cid] = [i for i in self.by_bot_ids[cid] if i > self.last_messages_ids[cid]]
        for msg in messages:
            event = _runner_module.NewMessageEvent(self._Runner__last_msg_event_tag, msg, stack)
            stack.add_events([event])
            result[cid].append(event)
    return result


Runner.generate_new_message_events = _patched_generate_new_message_events


def _patched_method(self, request_method, api_method, headers, payload,
                    exclude_phpsessid: bool = False, raise_not_200: bool = False):
    """Заменяет стандартный Account.method — шлёт полный набор cookies"""
    headers = dict(headers)
    gk = f"golden_key={self.golden_key}"
    php = f"PHPSESSID={self.phpsessid}" if self.phpsessid and not exclude_phpsessid else None
    clean_template = []
    for part in self._extra_cookie_template.split(";"):
        part = part.strip()
        if part and not part.startswith(("golden_key=", "PHPSESSID=")):
            clean_template.append(part)
    final_cookies = [gk]
    if php:
        final_cookies.append(php)
    final_cookies.extend(clean_template)
    headers["cookie"] = "; ".join(final_cookies)
    if self.user_agent:
        headers["user-agent"] = self.user_agent
    link = api_method if api_method.startswith("https://funpay.com") else "https://funpay.com/" + api_method
    response = getattr(requests, request_method)(
        link, headers=headers, data=payload,
        timeout=getattr(self, "requests_timeout", 10),
        proxies=getattr(self, "proxy", None) or {}
    )
    if response.status_code == 403:
        raise fp_exceptions.UnauthorizedError(response)
    elif response.status_code != 200 and raise_not_200:
        raise fp_exceptions.RequestFailedError(response)
    return response


def create_account(
    golden_key: str,
    user_agent: Optional[str] = None,
    extra_cookies: Optional[str] = None,
) -> Account:
    user_agent = user_agent or config.FUNPAY_USER_AGENT
    extra_cookies = extra_cookies or config.FUNPAY_EXTRA_COOKIES
    acc = Account(golden_key, user_agent=user_agent)
    acc._extra_cookie_template = extra_cookies
    acc.method = _types.MethodType(_patched_method, acc)
    acc.get()
    return acc


def test_connection(golden_key: str):
    try:
        acc = create_account(golden_key)
        return True, f"Авторизован как {acc.username} (ID: {acc.id})"
    except fp_exceptions.UnauthorizedError:
        return False, "Golden Key недействителен или ты вышел из аккаунта в браузере"
    except Exception as e:
        return False, str(e)