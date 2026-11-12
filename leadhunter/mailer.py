import asyncio
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid

from leadhunter.config import Settings


class Mailer:
    def __init__(self, settings: Settings) -> None:
        self.host = settings.smtp_host
        self.port = settings.smtp_port
        self.username = settings.smtp_username
        self.password = settings.smtp_password
        self.from_address = settings.smtp_from or settings.smtp_username
        self.from_name = settings.smtp_from_name

    @staticmethod
    def is_configured(settings: Settings) -> bool:
        return bool(settings.smtp_host and (settings.smtp_from or settings.smtp_username))

    def build_message(self, to: str, subject: str, text: str) -> EmailMessage:
        message = EmailMessage()
        message["From"] = formataddr((self.from_name, self.from_address))
        message["To"] = to
        message["Subject"] = subject
        message["Message-ID"] = make_msgid(domain=self.from_address.rsplit("@", 1)[-1])
        message.set_content(text)
        return message

    def _send_blocking(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self.port == 465:
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=30, context=context)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=30)
        with server:
            if self.port != 465:
                server.starttls(context=context)
            if self.username:
                server.login(self.username, self.password)
            server.send_message(message)

    async def send(self, to: str, subject: str, text: str) -> None:
        await asyncio.to_thread(self._send_blocking, self.build_message(to, subject, text))
