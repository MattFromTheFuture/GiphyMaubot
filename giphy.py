from typing import Type
import urllib.parse
import random
from mautrix.types import RoomID, ImageInfo
from mautrix.util.config import BaseProxyConfig, ConfigUpdateHelper
from maubot import Plugin, MessageEvent
from maubot.handlers import command


# Setup config file
class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper) -> None:
        helper.copy("giphy_api_key")
        helper.copy("klipy_api_key")
        helper.copy("klipy_api_version")
        helper.copy("provider")
        helper.copy("source")
        helper.copy("response_type")
        helper.copy("num_results")
        helper.copy("rating")
        helper.copy("max_gif_size_mb")


class GiphyPlugin(Plugin):
    async def start(self) -> None:
        await super().start()
        self.config.load_and_update()

    def max_gif_size_bytes(self) -> int:
        try:
            mb = float(self.config.get("max_gif_size_mb", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return int(mb * 1024 * 1024)

    def filter_by_size(self, results: list) -> list:
        # size limit only matters when we intend to upload the file
        max_bytes = self.max_gif_size_bytes()
        if not max_bytes or self.config.get("response_type", None) != "upload":
            return results
        return [
            g
            for g in results
            if int(g.get("images", {}).get("original", {}).get("size", 0)) <= max_bytes
        ]

    async def send_gif(
        self, room_id: RoomID, gif_link: str, query: str, info: dict
    ) -> None:
        resp = await self.http.get(gif_link)
        if resp.status != 200:
            self.log.warning(f"Unexpected status fetching image {gif_link}: {resp.status}")
            return None

        data = await resp.read()
        max_bytes = self.max_gif_size_bytes()
        if max_bytes and len(data) > max_bytes:
            await self.client.send_notice(
                room_id,
                f"Couldn't upload GIF ({len(data) / 1024 / 1024:.1f} MB, over "
                f"the {max_bytes / 1024 / 1024:.0f} MB limit), "
                f"here's a link instead:\n{gif_link}",
            )
            return None
        mime = info["mime"]
        filename = f"{query}.gif" if len(query) > 0 else "giphy.gif"
        uri = await self.client.upload_media(data, mime_type=mime, filename=filename)

        await self.client.send_image(
            room_id,
            url=uri,
            file_name=filename,
            info=ImageInfo(
                mimetype=info["mime"], width=info["width"], height=info["height"]
            ),
        )

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @command.new(
        "giphy",
        aliases=("gif", "g", "klipy"),
    )
    @command.argument("search_term", pass_raw=True, required=False)
    async def handler(self, evt: MessageEvent, search_term: str) -> None:
        await evt.mark_read()
        if not search_term:
            # If user doesn't supply a search term, set to empty string
            search_term = ""

        oversize_fallback = False

        if self.config["provider"] == "giphy":
            api_key = self.config["giphy_api_key"]
            rating = self.config["rating"]
            response_type = self.config["response_type"]
            if search_term:
                # Search and pick a random result, so repeated queries
                # don't always return the same top gif. The API returns
                # the top ~50 matches to sample from.
                endpoint = "https://api.giphy.com/v1/gifs/search"
                url_params = urllib.parse.urlencode(
                    {"q": search_term, "api_key": api_key, "rating": rating}
                )
                async with self.http.get(
                    f"{endpoint}?{url_params}"
                ) as response:
                    data = await response.json()
                try:
                    meta = data.get("meta") or {}
                    if meta.get("status") != 200:
                        self.log.warning(f"Giphy API returned error: {meta}")
                        await evt.respond("Something went wrong")
                        return None
                    results = data["data"]
                    if not results:
                        self.log.warning(
                            f"Giphy returned no results for '{search_term}': {meta}"
                        )
                        await evt.respond(
                            f"Couldn't find any gifs matching '{search_term}'"
                        )
                        return None
                    filtered = self.filter_by_size(results)
                    if not filtered:
                        # nothing fits the upload size limit, send a link instead
                        filtered = results
                        oversize_fallback = True
                    gif = random.choice(filtered)["images"]["original"]
                    gif_link = gif["url"]
                    info = {
                        "width": int(gif["width"]),
                        "height": int(gif["height"]),
                        "mime": "image/gif",  # this shouldn't really change
                    }
                except Exception:
                    self.log.exception("Failed to fetch/parse GIF response from Giphy")
                    await evt.respond("Something went wrong")
                    return None
            else:
                # No search term: use the configured random source (trending/random)
                source = self.config["source"]
                url_params = urllib.parse.urlencode({"api_key": api_key, "rating": rating})
                async with self.http.get(
                    "https://api.giphy.com/v1/gifs/{}?{}".format(source, url_params)
                ) as response:
                    data = await response.json()
                try:
                    gif = data["data"]
                    if isinstance(gif, list):
                        if not gif:
                            self.log.warning(
                                f"Giphy returned no results from '{source}': "
                                f"{data.get('meta')}"
                            )
                            await evt.respond("Couldn't find any gifs right now")
                            return None
                        # some endpoints (e.g. trending) return a list,
                        # pick one at random
                        filtered = self.filter_by_size(gif)
                        if not filtered:
                            filtered = gif
                            oversize_fallback = True
                        gif = random.choice(filtered)
                    gif = gif["images"]["original"]
                    gif_link = gif["url"]
                    info = {
                        "width": int(gif["width"]),
                        "height": int(gif["height"]),
                        "mime": "image/gif",  # this shouldn't really change
                    }
                except Exception:
                    self.log.exception("Failed to fetch/parse GIF response from provider")
                    await evt.respond("Something went wrong")
                    return None

        elif self.config["provider"] == "klipy":
            api_key = self.config["klipy_api_key"]
            rating = self.config["rating"]
            params = {"q": search_term, "rating": rating, "per_page": self.config["num_results"]}
            url_params = urllib.parse.urlencode(params)
            response_type = self.config["response_type"]
            async with self.http.get(
                f"https://api.klipy.com/api/v1/{api_key}/gifs/search?{url_params}"
            ) as response:
                data = await response.json()
            try:
                results = data["data"]["data"]
                image_num = random.randint(0, len(results) - 1)
                gif = results[image_num]["file"]["hd"]["gif"]
                gif_link = gif["url"]
                info = {
                    "width": int(gif["width"]),
                    "height": int(gif["height"]),
                    "mime": "image/gif",
                }
            except Exception:
                self.log.exception("Failed to fetch/parse GIF response from Klipy")
                await evt.respond("Something went wrong.")
                return None
        else:
            raise (Exception("Wrong provider:", self.config["provider"]))

        if response_type == "message":
            await evt.respond(gif_link, allow_html=True)  # Respond to user
        elif response_type == "reply":
            await evt.reply(gif_link, allow_html=True)  # Reply to user
        elif response_type == "upload" and oversize_fallback:
            await evt.respond(
                f"{gif_link}\n"
                f"(every match was over my "
                f"{self.max_gif_size_bytes() / 1024 / 1024:.0f} MB limit, "
                "so here's a link instead)"
            )
        elif response_type == "upload":
            await self.send_gif(
                evt.room_id, gif_link, search_term, info
            )  # Upload the GIF to the room
        else:
            await evt.respond(
                "something is wrong with my config, be sure to set a response_type"
            )
