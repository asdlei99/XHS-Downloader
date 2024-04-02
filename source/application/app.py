from asyncio import Event
from asyncio import Queue
from asyncio import QueueEmpty
from asyncio import gather
from asyncio import sleep
from contextlib import suppress
from datetime import datetime
from re import compile
from typing import Callable

from pyperclip import paste

from source.expansion import Converter
from source.expansion import Namespace
from source.module import DataRecorder
from source.module import IDRecorder
from source.module import Manager
from source.module import (
    ROOT,
    ERROR,
    WARNING,
    MASTER,
)
from source.module import Translate
from source.module import logging
from .download import Download
from .explore import Explore
from .image import Image
from .request import Html
from .video import Video

__all__ = ["XHS"]


class XHS:
    LINK = compile(r"https?://www\.xiaohongshu\.com/explore/[a-z0-9]+")
    SHARE = compile(r"https?://www\.xiaohongshu\.com/discovery/item/[a-z0-9]+")
    SHORT = compile(r"https?://xhslink\.com/[A-Za-z0-9]+")
    __INSTANCE = None

    def __new__(cls, *args, **kwargs):
        if not cls.__INSTANCE:
            cls.__INSTANCE = super().__new__(cls)
        return cls.__INSTANCE

    def __init__(
            self,
            work_path="",
            folder_name="Download",
            user_agent: str = None,
            cookie: str = None,
            proxy: str = None,
            timeout=10,
            chunk=1024 * 1024,
            max_retry=5,
            record_data=False,
            image_format="PNG",
            image_download=True,
            video_download=True,
            folder_mode=False,
            language="zh_CN",
            server=False,
            transition: Callable[[str], str] = None,
            *args,
            **kwargs,
    ):
        self.message = transition or Translate(language).message()
        self.manager = Manager(
            ROOT,
            work_path,
            folder_name,
            user_agent,
            chunk,
            cookie,
            proxy,
            timeout,
            max_retry,
            record_data,
            image_format,
            folder_mode,
            self.message,
        )
        self.html = Html(self.manager)
        self.image = Image()
        self.video = Video()
        self.explore = Explore()
        self.convert = Converter()
        self.download = Download(self.manager)
        self.id_recorder = IDRecorder(self.manager)
        self.data_recorder = DataRecorder(self.manager)
        self.clipboard_cache: str = ""
        self.queue = Queue()
        self.event = Event()

    def __extract_image(self, container: dict, data: Namespace):
        container["下载地址"] = self.image.get_image_link(
            data, self.manager.image_format)

    def __extract_video(self, container: dict, data: Namespace):
        container["下载地址"] = self.video.get_video_link(data)

    async def __download_files(self, container: dict, download: bool, index, log, bar):
        name = self.__naming_rules(container)
        if (u := container["下载地址"]) and download:
            if await self.skip_download(i := container["作品ID"]):
                logging(
                    log, self.message("作品 {0} 存在下载记录，跳过下载").format(i))
            else:
                path, result = await self.download.run(u, index, name, container["作品类型"], log, bar)
                await self.__add_record(i, result)
        elif not u:
            logging(log, self.message("提取作品文件下载地址失败"), ERROR)
        await self.save_data(container)

    async def save_data(self, data: dict, ):
        data["采集时间"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data["下载地址"] = " ".join(data["下载地址"])
        await self.data_recorder.add(**data)

    async def __add_record(self, id_: str, result: tuple) -> None:
        if all(result):
            await self.id_recorder.add(id_)

    async def extract(self,
                      url: str,
                      download=False,
                      index: list | tuple = None,
                      log=None,
                      bar=None) -> list[dict]:
        # return  # 调试代码
        urls = await self.__extract_links(url, log)
        if not urls:
            logging(log, self.message("提取小红书作品链接失败"), WARNING)
        else:
            logging(
                log, self.message("共 {0} 个小红书作品待处理...").format(len(urls)))
        # return urls  # 调试代码
        return [await self.__deal_extract(i, download, index, log, bar, ) for i in urls]

    async def extract_cli(self,
                          url: str,
                          download=True,
                          index: list | tuple = None,
                          log=None,
                          bar=None) -> None:
        url = await self.__extract_links(url, log)
        if not url:
            logging(log, self.message("提取小红书作品链接失败"), WARNING)
        else:
            await self.__deal_extract(url[0], download, index, log, bar)

    async def __extract_links(self, url: str, log) -> list:
        urls = []
        for i in url.split():
            if u := self.SHORT.search(i):
                i = await self.html.request_url(
                    u.group(), False, log)
            if u := self.SHARE.search(i):
                urls.append(u.group())
            elif u := self.LINK.search(i):
                urls.append(u.group())
        return urls

    async def __deal_extract(self, url: str, download: bool, index: list | tuple | None, log, bar):
        logging(log, self.message("开始处理作品：{0}").format(url))
        html = await self.html.request_url(url, log=log)
        namespace = self.__generate_data_object(html)
        if not namespace:
            logging(log, self.message("{0} 获取数据失败").format(url), ERROR)
            return {}
        data = self.explore.run(namespace)
        # logging(log, data)  # 调试代码
        if not data:
            logging(log, self.message("{0} 提取数据失败").format(url), ERROR)
            return {}
        match data["作品类型"]:
            case "视频":
                self.__extract_video(data, namespace)
            case "图文":
                self.__extract_image(data, namespace)
            case _:
                data["下载地址"] = []
        await self.__download_files(data, download, index, log, bar)
        logging(log, self.message("作品处理完成：{0}").format(url))
        return data

    def __generate_data_object(self, html: str) -> Namespace:
        data = self.convert.run(html)
        return Namespace(data)

    def __naming_rules(self, data: dict) -> str:
        time_ = data["发布时间"].replace(":", ".")
        author = self.manager.filter_name(data["作者昵称"]) or data["作者ID"]
        title = self.manager.filter_name(data["作品标题"]) or data["作品ID"]
        return f"{time_}_{author}_{title[:64]}"

    async def monitor(self, delay=1, download=False, log=None, bar=None) -> None:
        logging(
            None,
            self.message(
                "程序会自动读取并提取剪贴板中的小红书作品链接，并自动下载链接对应的作品文件，如需关闭，请点击关闭按钮，或者向剪贴板写入 “close” 文本！"),
            style=MASTER,
        )
        self.event.clear()
        await gather(self.__push_link(delay), self.__receive_link(delay, download, None, log, bar))

    async def __push_link(self, delay: int):
        while not self.event.is_set():
            if (t := paste()).lower() == "close":
                self.stop_monitor()
            elif t != self.clipboard_cache:
                self.clipboard_cache = t
                [await self.queue.put(i) for i in await self.__extract_links(t, None)]
            await sleep(delay)

    async def __receive_link(self, delay: int, *args, **kwargs):
        while not self.event.is_set() or self.queue.qsize() > 0:
            with suppress(QueueEmpty):
                await self.__deal_extract(self.queue.get_nowait(), *args, **kwargs)
            await sleep(delay)

    def stop_monitor(self):
        self.event.set()

    async def skip_download(self, id_: str) -> bool:
        return bool(await self.id_recorder.select(id_))

    async def __aenter__(self):
        await self.id_recorder.__aenter__()
        await self.data_recorder.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.id_recorder.__aexit__(exc_type, exc_value, traceback)
        await self.data_recorder.__aexit__(exc_type, exc_value, traceback)
        await self.close()

    async def close(self):
        await self.manager.close()
