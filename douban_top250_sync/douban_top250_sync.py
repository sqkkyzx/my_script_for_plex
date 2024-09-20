import argparse
import difflib
import math
import time

import yaml
from lxml import html
import httpx
import asyncio
from plexapi.myplex import PlexServer
import logging
import sqlite3
from colorama import Fore

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s\t%(message)s'
)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/58.0.3029.110 Safari/537.3',
    'Referer': 'https://www.douban.com/',
}


def remove_punctuation(text):
    translator = str.maketrans('', '', ' !()-[]{};:\'\",<>./?@#$%^&*_~‧！，。：；”“’‘\\？《》~·-——=+、|{}【】\xa0')
    return str(text).strip().translate(translator).strip()


def loadconfig():
    def load_form_file(yaml_file_path):
        try:
            with open(yaml_file_path, 'r', encoding='utf-8') as file:
                data = yaml.safe_load(file)

                class _cfg:
                    configfile = yaml_file_path
                    baseurl = data['auth']['baseurl']
                    token = data['auth']['token']
                    playlist = data['playlist']

                return _cfg
        except Exception as e:
            logging.debug(e)
            return False

    def load_from_args():
        parser = argparse.ArgumentParser(description="一个接受基本 URL 作为参数的脚本。")
        parser.add_argument('--configfile', default="config.yaml", type=str, required=False, help="配置文件路径")
        parser.add_argument('--playlist', default="", type=str, required=False, help="A Doulist ID")
        parser.add_argument('--baseurl', default="", type=str, required=False,
                            help="Plex 地址，例如 http://127.0.0.1:32400")
        parser.add_argument('--token', default="", type=str, required=False, help="Plex Token")
        args = parser.parse_args()

        class _cfg:
            configfile = args.configfile
            baseurl = args.baseurl
            token = args.token
            playlist = [args.playlist]

        return _cfg

    args_cfg = load_from_args()
    file_cfg = load_form_file(args_cfg.configfile)

    if file_cfg:
        return file_cfg
    else:
        if not args_cfg.baseurl or not args_cfg.token:
            raise "未提供参数或配置文件。"
        return args_cfg


def ensure_table_exists(playlist_id):
    # 连接到当前目录下的 listcache.db 数据库（如果不存在则创建一个新的数据库）
    conn = sqlite3.connect('listcache.db')
    cursor = conn.cursor()
    table_name = f'id_{playlist_id}'

    try:
        # 检查是否存在名为 playlist 的表
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='playlist';")
        table_exists = cursor.fetchone()
        if not table_exists:
            # 如果表不存在，创建一个新的表，结构为：id, playlist_name, renew_time
            cursor.execute(f"""
                CREATE TABLE {table_name} (
                    id TEXT PRIMARY KEY,
                    name TEXT,
                    renew_time INTEGER
                );
            """)
            conn.commit()

        # 检查是否存在名为 table_name 的表
        cursor.execute(f"SELECT name FROM sqlite_master WHERE type='table' AND name='{table_name}';")
        table_exists = cursor.fetchone()

        if not table_exists:
            # 如果表不存在，创建一个新的表，结构为：id, title, original_title, year, imdbid, tmdbid
            cursor.execute(f"""
                CREATE TABLE {table_name} (
                    id INTEGER PRIMARY KEY,
                    title TEXT,
                    original_title TEXT,
                    year TEXT,
                    imdbid TEXT,
                    tmdbid TEXT
                );
            """)
            conn.commit()
            return False

        # 检查表中是否存在数据
        cursor.execute(f"SELECT COUNT(*) FROM {table_name};")
        row_count = cursor.fetchone()[0]

        if row_count <= 0:
            return False

        # 获取表中的所有行数据
        cursor.execute(f"SELECT * FROM {table_name};")
        playlist_data = cursor.fetchall()

        return playlist_data

    except sqlite3.Error as e:
        logging.debug(e)
        return False

    finally:
        # 关闭数据库连接
        conn.close()


def insert_data(playlist_id, playlist_name, data):
    # 连接到当前目录下的 listcache.db 数据库
    conn = sqlite3.connect('listcache.db')
    cursor = conn.cursor()
    table_name = f'id_{playlist_id}'

    try:
        # 清空指定的表
        cursor.execute(f"DELETE FROM {table_name};")
        conn.commit()
        logging.info(f"已清空 '{table_name}' 的缓存数据。")

        cursor.executemany(
            f"INSERT INTO {table_name} (id, title, original_title, year, imdbid, tmdbid) VALUES (?, ?, ?, ?, ?, ?);",
            data)

        # 插入或更新 playlist 表中的数据
        cursor.execute(
            "INSERT INTO playlist (id, name, renew_time) VALUES (?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, renew_time=excluded.renew_time;",
            (str(playlist_id), str(playlist_name), int(time.time())))

        conn.commit()
        logging.info(f"已将 {table_name} 的数据缓存。")

    except sqlite3.Error as e:
        logging.debug(e)

    finally:
        # 关闭数据库连接
        conn.close()


def get_playlistname(playlist_id):
    # 连接到当前目录下的 listcache.db 数据库
    conn = sqlite3.connect('listcache.db')
    cursor = conn.cursor()
    cursor.execute(f"SELECT name, renew_time FROM playlist WHERE id = '{playlist_id}';")
    name, renew_time = cursor.fetchone()
    timestring = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(renew_time))
    logging.info(f'播放列表 {name} 上次更新的时间为 {timestring}')
    return name


async def fetch_datas(urls):
    async with httpx.AsyncClient() as client:
        tasks = [client.get(url, headers=HEADERS, timeout=2000, follow_redirects=True) for url in urls]
        responses = await asyncio.gather(*tasks)
        html_docs = [response.text for response in responses]
    return html_docs


def get_douban_playlist(playlist_id='top250', renew: bool = False):
    cache = ensure_table_exists(playlist_id)

    if renew or not cache:
        if playlist_id == 'top250':
            url = f"https://movie.douban.com/top250"
            page = 10
            playlist_name = '豆瓣TOP250'
        else:
            url = f"https://www.douban.com/doulist/{playlist_id}/"
            firstpage = httpx.get(url=url, headers=HEADERS, follow_redirects=True).text

            item_count_text = html.fromstring(firstpage).xpath('//a[contains(@class, "active")]/span/text()')[0]
            item_count = int(remove_punctuation(item_count_text))

            page = math.ceil(item_count / 25)
            playlist_name = html.fromstring(firstpage).xpath('//title/text()')[0]

        # 获取全部页面的内容
        urls = [f"{url}?start={start_index}" for start_index in [i * 25 for i in range(0, page)]]
        html_docs = asyncio.run(fetch_datas(urls))
        playlist = []

        if playlist_id == 'top250':
            itemlist = html.fromstring('\n'.join(html_docs)).xpath('//ol[contains(@class, "grid_view")]/li/div')
            for index, item in enumerate(itemlist):
                index = item.xpath('./div[contains(@class, "pic")]/em/text()')[0]
                title = item.xpath('./div[contains(@class, "info")]/div/a/span[contains(@class, "title")]/text()')
                local_title = title[0]
                origin_title = title[1].replace('/', '').strip() if len(title) > 1 else None
                bd = item.xpath('./div[contains(@class, "info")]/div[contains(@class, "bd")]/p/text()')
                year = remove_punctuation(bd[1].split('/')[0]) if len(bd) > 1 else None
                if len(year) > 4:
                    year = year[0:4]

                media = (index, local_title, origin_title, year, None, None)
                playlist.append(media)
        else:
            itemlist = html.fromstring('\n'.join(html_docs)).xpath('//div[contains(@class, "bd doulist-subject")]')
            for index, item in enumerate(itemlist):
                title = item.xpath('./div[contains(@class, "title")]/a/text()')
                local_title = title[0].strip().split(' ')[0] if title[0].strip() else title[1].strip().split(' ')[0]

                year_xml = item.xpath('./div[contains(@class, "abstract")]/text()')
                year = remove_punctuation(year_xml[-1].replace('年份', '')) if len(year_xml) > 1 else None

                media = (index, local_title, None, year, None, None)
                playlist.append(media)

        insert_data(playlist_id, playlist_name, playlist)
        return playlist_name, playlist
    else:
        playlist_name = get_playlistname(playlist_id)
        return playlist_name, cache


def list_media(pms_client, allow_libs):
    medias = []
    for library in pms_client.library.sections():
        allowed_libtypes = allow_libs.get(library.type, [])
        for libtype in allowed_libtypes:
            _list = library.search(libtype=libtype)
            medias.extend(_list)
    media_list = [(media, str(media.title), None, str(media.year), None, None) for media in medias]
    return media_list


def main(baseurl: str, token: str, playlist_id='top250', renew=False):
    try:
        client = PlexServer(baseurl, token)
    except Exception as e:
        logging.debug(e)
        raise "连接服务器失败，检查 token 和 baseurl，或者确认 plex 是否运行。"

    medias = list_media(client, {'movie': ['movie']})
    logging.warning(f'{Fore.GREEN}电影库中共计 {len(medias)} 个电影。{Fore.RESET}')

    playlist_id = str(playlist_id)

    playlist_name, douban_playlist = get_douban_playlist(playlist_id, renew)
    logging.warning(f'{Fore.GREEN}已成功获取 {playlist_name} 中的 {len(douban_playlist)} 个电影。{Fore.RESET}')

    try:
        playlist = client.playlist(title=playlist_name)
        playlist.delete()
    except Exception as e:
        logging.debug(e)

    playlist_media_items = []

    for item in douban_playlist:
        temp_index = None
        temp_media = None
        temp_ratio = 0
        temp_deviation = 999

        for index, media in enumerate(medias):

            try:
                year_deviation = abs(int(item[3]) - int(media[3]))
                # 首先排除年份差距超过 2 年的
                if year_deviation > 2:
                    continue
            except Exception as e:
                logging.debug(e)
                continue

            # top_title = f'{remove_punctuation(item[1])} ({item[3]})'
            # media_title = f'{remove_punctuation(media[1])} ({media[3]})'

            top_title = remove_punctuation(item[1]) + item[3]
            media_title = remove_punctuation(media[1]) + media[3]

            top_fill = '*' * (15-len(top_title))
            media_fill = '*' * (15 - len(media_title))
            ratio = difflib.SequenceMatcher(None, top_title+top_fill, media_title+media_fill).quick_ratio()

            # 等于 1 或名称完全相同，但年代误差小于 2 ，视为匹配，并结束查询
            if ratio == 1:
                temp_index, temp_media, temp_ratio, temp_deviation = index, media[0], ratio, year_deviation
                break

            # 大于 0.8 视为模糊匹配，并试图寻找下一个更相似的匹配
            if ratio >= 0.8 and ratio > temp_ratio and year_deviation <= 2 and top_title[0] == media_title[0]:
                temp_index, temp_media, temp_ratio, temp_deviation = index, media[0], ratio, year_deviation
                # logging.info(f'  \t{Fore.MAGENTA}可能的匹配：{top_title} 与 {media_title} 相似度 {ratio}{Fore.RESET}')

        # 完全相同，视为精准匹配
        if temp_media and temp_ratio == 1.0:
            logging.info(f'{Fore.GREEN}{item[0]}\t{item[1]}({item[3]}) 精准匹配成功。{Fore.RESET}')
            playlist_media_items.append(temp_media)
            medias.pop(temp_index)
        # 名称相同但上映年份差不超过2，视为模糊匹配
        elif temp_media and item[1] == temp_media.title and temp_deviation <= 2:
            logging.error(f'{Fore.YELLOW}{item[0]}\t{item[1]}({item[3]}) 模糊匹配为 '
                          f'{temp_media.title}({temp_media.year}) ，相似度 {temp_ratio} {Fore.RESET}')
            playlist_media_items.append(temp_media)
            medias.pop(temp_index)
        # 名称相似度大于0.8，且上映年份差为0，视为模糊匹配
        elif temp_media and temp_ratio >= 0.8 and temp_deviation == 0:
            logging.error(f'{Fore.YELLOW}{item[0]}\t{item[1]}({item[3]}) 模糊匹配为 '
                          f'{temp_media.title}({temp_media.year}) ，相似度 {temp_ratio} {Fore.RESET}')
            playlist_media_items.append(temp_media)
            medias.pop(temp_index)
        # 名称相似度大于0.9，上映年份差不大于1，但名称长度相同的，视为模糊匹配
        elif temp_media and temp_ratio >= 0.9 and len(item[1]) == len(temp_media.title):
            logging.error(f'{Fore.YELLOW}{item[0]}\t{item[1]}({item[3]}) 模糊匹配为 '
                          f'{temp_media.title}({temp_media.year}) ，相似度 {temp_ratio} {Fore.RESET}')
            playlist_media_items.append(temp_media)
            medias.pop(temp_index)
        elif not temp_media:
            logging.error(f'{Fore.RED}{item[0]}\t{item[1]}({item[3]}) 在库中不存在。{Fore.RESET}')
        else:
            logging.error(f'{Fore.RED}{item[0]}\t可能的匹配(不会加入列表)：{item[1]}({item[3]}) 与 '
                          f'{temp_media.title}({temp_media.year}) 相似度 {temp_ratio} {Fore.RESET}')

    if playlist_media_items:
        client.createPlaylist(title=playlist_name, items=playlist_media_items)
        logging.warning(f'{Fore.CYAN}共计匹配到 {len(playlist_media_items)} 个项目。{Fore.RESET}')
    else:
        logging.error('没有匹配到该列表中的任何电影。')


if __name__ == '__main__':
    Config = loadconfig()
    for playlist_id in Config.playlist:
        main(Config.baseurl, Config.token, playlist_id)
        time.sleep(2)
        print('\n\n\n')
        time.sleep(2)
