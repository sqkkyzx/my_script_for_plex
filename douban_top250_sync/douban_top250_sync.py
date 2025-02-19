import argparse
import difflib
import json
import math
import time
import re
import logging
import asyncio
from pathlib import Path
from typing import Dict, List, Tuple

import yaml
import httpx
from lxml import html
from colorama import Fore
from pypinyin import pinyin
from plexapi.myplex import PlexServer


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s\t%(message)s'
)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3',
    'Referer': 'https://www.douban.com/',
}


def remove_punctuation(text):
    """
    不会处理 : 和 ：
    :param text:
    :return:
    """
    text = str(text).strip().replace(' ', '：').replace(":", "：")
    translator = str.maketrans('', '', ' !()-[]{};\'\",<>./?@#$%^&*_~‧！，。；”“’‘\\？《》~·-——=+、|{}【】\xa0')
    return text.translate(translator).strip()


def split_movie_name(movie_name):
    parts = movie_name.split("：")

    if len(parts) == 2:
        main_title = parts[0]
        part_title = parts[1]
    else:
        main_title = movie_name
        part_title = ''

    # 处理结尾数字的情况
    match = re.search(r'(\D+)(\d+)$', main_title)
    if match:
        main_title = match.group(1)
        part_index = match.group(2)
    else:
        part_index = ''

    return main_title, part_index, part_title


def loadconfig():
    with open("config.yaml", 'r', encoding='utf-8') as file:
        data = yaml.safe_load(file)

        class Cfg:
            baseurl = data['auth']['baseurl']
            token = data['auth']['token']
            playlist = data['playlist']

        return Cfg


def get_playlist_name() -> dict|None:

    playlist_name_cache_path = Path('playlist_cache') / 'playlist'
    playlist_name_cache = {'top250': '豆瓣TOP250'}

    if not playlist_name_cache_path.exists():
        with playlist_name_cache_path.open('w', encoding='utf-8') as f:
            f.write(json.dumps(playlist_name_cache, ensure_ascii=False, indent=4))
    else:
        with playlist_name_cache_path.open('r', encoding='utf-8') as f:
            playlist_name_cache = json.load(f)

    return playlist_name_cache


async def fetch_datas(urls):
    async with httpx.AsyncClient(proxy="http://127.0.0.1:10809") as client:
        tasks = [client.get(url, headers=HEADERS, timeout=2000, follow_redirects=True) for url in urls]
        responses = await asyncio.gather(*tasks)
        html_docs = [response.text for response in responses]
    return html_docs


def get_douban_playlist(playlist_id:str='top250', playlist_name:str='豆瓣TOP250', renew: bool = False) -> Tuple[str, List[Dict[str,str|None]]]:
    playlist_data_cache_path = Path('playlist_cache') / f'{playlist_id}.json'

    playlist:List[Dict[str,str|None]] = []

    # 如果缓存存在并且无需更新，则直接返回结果
    if not renew and playlist_data_cache_path.exists():
        with open(playlist_data_cache_path, 'r', encoding='utf-8') as f:
            playlist = json.loads(f.read())
        return playlist_name, playlist


    logging.info(playlist_id)
    if playlist_id == 'top250':
        playlist_name = '豆瓣TOP250'

        # 获取全部页面的内容
        urls = [f"https://movie.douban.com/top250?start={start_index}" for start_index in [i * 25 for i in range(0, 10)]]
        html_docs = asyncio.run(fetch_datas(urls))

        itemlist = html.fromstring('\n'.join(html_docs)).xpath('//ol[contains(@class, "grid_view")]/li/div')

        for index, item in enumerate(itemlist):
            index = item.xpath('./div[contains(@class, "pic")]/em/text()')[0]
            title = item.xpath('./div[contains(@class, "info")]/div/a/span[contains(@class, "title")]/text()')
            local_title = title[0]
            origin_title = title[1].replace('/', '').strip() if len(title) > 1 else None
            bd = item.xpath('./div[contains(@class, "info")]/div[contains(@class, "bd")]/p/text()')
            year = remove_punctuation(bd[1].split('/')[0]) if len(bd) > 1 else None
            year = year[0:4] if len(year) > 4 else year
            media = {'index': index, 'local_title': local_title, 'origin_title': origin_title, 'year': year, 'tmdb-id': None, 'imdb-id': None,}
            playlist.append(media)
    else:
        url = f"https://www.douban.com/doulist/{playlist_id}/"
        print(url)
        first_page = httpx.get(url=url, headers=HEADERS, follow_redirects=True).text

        item_count_text = html.fromstring(first_page).xpath('//a[contains(@class, "active")]/span/text()')[0]
        item_count = int(remove_punctuation(item_count_text))

        page = math.ceil(item_count / 25)

        # 获取全部页面的内容
        urls = [f"{url}?start={start_index}" for start_index in [i * 25 for i in range(0, page)]]
        html_docs = asyncio.run(fetch_datas(urls))
        playlist = []

        itemlist = html.fromstring('\n'.join(html_docs)).xpath('//div[contains(@class, "bd doulist-subject")]')
        for index, item in enumerate(itemlist):
            title = item.xpath('./div[contains(@class, "title")]/a/text()')
            local_title = title[0].strip().split(' ')[0] if title[0].strip() else title[1].strip().split(' ')[0]

            year_xml = item.xpath('./div[contains(@class, "abstract")]/text()')
            year = remove_punctuation(year_xml[-1].replace('年份', '').replace('：', '').replace(':', '')) if len(year_xml) > 1 else None
            media = {'index': index, 'local_title': local_title, 'origin_title': None, 'year': year, 'tmdb-id': None, 'imdb-id': None,}
            playlist.append(media)

    with open(playlist_data_cache_path, 'w', encoding='utf-8') as f:
        json_string = json.dumps(playlist, ensure_ascii=False, indent=4)
        f.write(json_string)

    return playlist_name, playlist


def list_media(pms_client, allow_libs):
    """
    列出媒体库项目
    :param pms_client: PLEX链接实例
    :param allow_libs: 支持的媒体库类型
    :return:
    """
    medias = []
    for library in pms_client.library.sections():
        allowed_lib_types = allow_libs.get(library.type, [])
        for libtype in allowed_lib_types:
            _list = library.search(libtype=libtype)
            medias.extend(_list)
    media_list = [(media, str(media.title), None, str(media.year), None, None) for media in medias]
    return media_list


def main(baseurl: str, token: str, douban_playlist: Dict[str,str], renew=False):
    try:
        client = PlexServer(baseurl, token)
    except Exception as e:
        logging.debug(e)
        raise "连接服务器失败，检查 token 和 baseurl，或者确认 plex 是否运行。"

    medias = list_media(client, {'movie': ['movie']})
    logging.info(f'{Fore.GREEN}电影库中共计 {len(medias)} 个电影。{Fore.RESET}')

    douban_playlist_id = douban_playlist["id"]
    douban_playlist_name = douban_playlist["name"]

    # 获取豆瓣列表
    playlist_name, douban_playlist = get_douban_playlist(str(douban_playlist_id), douban_playlist_name, renew)
    logging.info(f'{Fore.GREEN}已成功获取 {playlist_name} 中的 {len(douban_playlist)} 个电影。{Fore.RESET}')

    try:
        playlist = client.playlist(title=playlist_name)
        playlist.delete()
    except Exception as e:
        logging.debug(e)

    playlist_media_items = []

    # 遍历豆瓣列表和媒体库
    for douban_item in douban_playlist:
        douban_item_index = douban_item['index']
        douban_item_title = douban_item['local_title']
        douban_item_year = douban_item['year']

        temp_index = None
        temp_media = None
        temp_ratio = 0
        massage = ''

        for index, media in enumerate(medias):
            try:
                list_movie, list_year = remove_punctuation(douban_item_title), douban_item_year
                plex_movie, plex_year = remove_punctuation(media[1]), media[3]

                list_title, list_index, list_part = split_movie_name(list_movie)
                plex_title, plex_index, plex_part = split_movie_name(plex_movie)
                year_deviation = abs(int(list_year) - int(plex_year))

            # 1. 跳过无法提取信息的
            except Exception as e:
                logging.debug(e)
                continue

            # 2. 跳过上映年份差距大于2年的
            if year_deviation > 2:
                continue
            # 3. 跳过有集数但集数不同的情况
            elif list_index != plex_index and list_index and plex_index:
                continue
            # 4. 跳过名称长度不一致的情况
            elif len(list_title) != len(plex_title):
                continue
            # # 4. 跳过名称第一个字拼音不一样的情况
            # elif pinyin(list_title[0]) != pinyin(plex_title[0]):
            #     continue
            else:
                pass

            list_fill_string = list_movie.replace('：', '') + '*' * (11 - len(list_movie)) + list_year
            plex_fill_string = plex_movie.replace('：', '') + '*' * (11 - len(plex_movie)) + plex_year

            ratio = difflib.SequenceMatcher(None, list_fill_string, plex_fill_string).quick_ratio()

            # 1. 相似度 = 1 视为完全匹配，结束查询
            if ratio == 1:
                temp_index, temp_media, temp_ratio, temp_deviation = index, media[0], ratio, year_deviation
                massage = '精准匹配'
                break
            # 2. 名称完全相同，年份完全相同的，视为完全匹配，结束查询
            #    即忽略副标题不同，或片名没有写第几部的情况
            if list_title == plex_title and list_year == plex_year:
                temp_index, temp_media, temp_ratio, temp_deviation = index, media[0], ratio, year_deviation
                if list_part != plex_part and list_index == plex_index:
                    massage = '分集名称不同'
                elif list_part == plex_part and list_index != plex_index:
                    massage = '集数不同'
                else:
                    massage = '集数与分集名称都不同'
                break
            # 3. 相似度 > 0.8 ，名称拼音完全相同，年份完全相同的，视为完全匹配，结束查询
            #    即忽略标题简繁不一、副标题不同，或片名没有写第几部的情况，
            if ratio > 0.8 and pinyin(list_title) == pinyin(plex_title) and list_year == plex_year:
                temp_index, temp_media, temp_ratio, temp_deviation = index, media[0], ratio, year_deviation
                if list_part == plex_part and list_index == plex_index:
                    massage = '简繁不一致'
                elif list_part != plex_part and list_index == plex_index:
                    massage = '简繁不一致且分集名称不同'
                elif list_part == plex_part and list_index != plex_index:
                    massage = '简繁不一致且集数不同'
                else:
                    massage = '简繁不一致且集数和分集名称都不同'
                break
            # 4. 相似度 > 0.8 ，视为模糊匹配，并试图寻找下一个更相似的匹配
            if ratio > 0.8 and ratio > temp_ratio:
                temp_index, temp_media, temp_ratio, temp_deviation = index, media[0], ratio, year_deviation
                num_trans = str.maketrans('123456789', '一二三四五六七八九')

                # a. 如果电影名完全相同，集数也相同，上映年份相差不到两年
                #    即忽略分集标题差异，忽略上映年份的小差异
                if list_title == plex_title and list_index == plex_index:
                    if list_part == plex_part:
                        massage = '上映年份不同'
                    else:
                        massage = '上映年份和分集名称都不同'

                # a. 如果电影名完全除了阿拉伯数字之外相同，集数也相同，上映年份相差不到两年
                #    即忽略分集标题差异，忽略上映年份的小差异
                if list_title.translate(num_trans) == plex_title.translate(num_trans) and list_index == plex_index:
                    massage = '名称中存在阿拉伯数字'

        def gen_log_msg():

            _item_print = f'{douban_item_index}\t{douban_item_title}({douban_item_year})'

            if not temp_media:
                return f'{Fore.RED}{_item_print} 不存在。{Fore.RESET}'

            _plex_print = f'{temp_media.title}({temp_media.year})'

            if massage == '精准匹配':
                return f'{Fore.GREEN}{_item_print} 精准匹配。{Fore.RESET}'
            elif massage:
                return (f'{Fore.CYAN}{_item_print} 与 {_plex_print} 模糊匹配(相似度{round(temp_ratio, 2)})，'
                        f'存在问题：{massage}。{Fore.RESET}')
            else:
                return (f'{Fore.RED}{_item_print} 与 {_plex_print} 模糊匹配(相似度{round(temp_ratio, 2)})，'
                        f'但未命中匹配规则，不会加入列表中。{Fore.RESET}')

        # 完全相同
        if temp_media and temp_ratio == 1.0 and massage:
            logging.info(gen_log_msg())
            playlist_media_items.append(temp_media)
            medias.pop(temp_index)
        # 模糊匹配
        elif temp_media and massage:
            logging.error(gen_log_msg())
            playlist_media_items.append(temp_media)
            medias.pop(temp_index)
        # 模糊匹配不入列
        elif temp_media and not massage:
            logging.error(gen_log_msg())
        # 无匹配
        else:
            logging.error(gen_log_msg())

    if playlist_media_items:
        client.createPlaylist(title=playlist_name, items=playlist_media_items)
        logging.warning(f'{Fore.CYAN}共计匹配到 {len(playlist_media_items)}/{len(douban_playlist)} 个项目。{Fore.RESET}')
    else:
        logging.error('没有匹配到该列表中的任何电影。')


if __name__ == '__main__':
    Config = loadconfig()
    Renew = False
    for op_playlist in Config.playlist:
        main(Config.baseurl, Config.token, op_playlist, Renew)
        if Renew:
            time.sleep(5)
        else:
            time.sleep(0.5)
        print('\n\n\n')
