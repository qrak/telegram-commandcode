"""Media handling: auto-attaching files the agent mentions in its output."""

from telegram_commandcode.gateway.gateway import BotGateway


class FakeBot:
    def __init__(self):
        self.photos = []
        self.documents = []

    async def send_photo(self, chat_id, photo, **kwargs):
        self.photos.append(chat_id)

    async def send_document(self, chat_id, document, **kwargs):
        self.documents.append(chat_id)


async def test_image_is_sent_as_photo(tmp_path):
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")

    bot = FakeBot()
    await BotGateway().media.auto_send_files(1, f"MEDIA:{image}", bot=bot)

    assert bot.photos == [1]
    assert bot.documents == []


async def test_pdf_is_sent_as_document(tmp_path):
    doc = tmp_path / "report.pdf"
    doc.write_bytes(b"pdf")

    bot = FakeBot()
    await BotGateway().media.auto_send_files(1, f"MEDIA:{doc}", bot=bot)

    assert bot.documents == [1]
    assert bot.photos == []


async def test_missing_file_is_ignored(tmp_path):
    bot = FakeBot()
    await BotGateway().media.auto_send_files(1, f"MEDIA:{tmp_path / 'nope.png'}", bot=bot)
    assert bot.photos == [] and bot.documents == []


async def test_no_bot_is_a_no_op(tmp_path):
    image = tmp_path / "x.png"
    image.write_bytes(b"png")
    await BotGateway().media.auto_send_files(1, f"MEDIA:{image}", bot=None)


async def test_text_without_paths_sends_nothing():
    bot = FakeBot()
    await BotGateway().media.auto_send_files(1, "all good, no files here", bot=bot)
    assert bot.photos == [] and bot.documents == []
