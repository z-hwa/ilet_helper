"""同時啟動雅斯寶寶與德語寶寶（同一個程序，在 Discord 上是兩個 bot）。

    python run_all.py

- 雅斯寶寶：DISCORD_TOKEN
- 德語寶寶：GERMAN_DISCORD_TOKEN（沒設定就只啟動雅斯寶寶）
一隻 bot 登入失敗（例如 token 填錯）不會影響另一隻。
"""
import asyncio
import logging
import os

import bot as ielts

log = logging.getLogger("run_all")


async def run_one(name, client, token):
    try:
        await client.start(token)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("%s 啟動失敗", name)


async def main():
    ielts.prepare()
    jobs = {"雅斯寶寶": (ielts.bot, ielts.DISCORD_TOKEN)}

    german_token = os.getenv("GERMAN_DISCORD_TOKEN", "").strip()
    if german_token:
        import german  # 晚一點才載入：沒設德語 token 時不需要德語相關套件

        german.core.init_db()  # 德語資料表已由雅斯寶寶的 init_db 一併建立；這裡確保順序無誤
        jobs["德語寶寶"] = (german.bot, german_token)
    else:
        log.warning("沒有設定 GERMAN_DISCORD_TOKEN，只啟動雅斯寶寶。")

    tasks = [asyncio.create_task(run_one(name, client, token)) for name, (client, token) in jobs.items()]
    try:
        await asyncio.gather(*tasks)
    finally:
        for _, (client, _) in jobs.items():
            if not client.is_closed():
                await client.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
