import os
import random
import sqlite3
from datetime import date, datetime, timedelta
from contextlib import closing

from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.all import Star, Context, AstrBotConfig, logger
from astrbot.api.star import StarTools
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent


class DailySign(Star):
    """每日发情插件 - 支持连续发情加成、积分排行、发情信息查询"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.db_path = os.path.join(str(StarTools.get_data_dir()), "daily_sign.db")
        self._init_db()
        logger.info("每日发情插件已加载")

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    def _init_db(self):
        """初始化 SQLite 数据库表结构"""
        with closing(sqlite3.connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute('''
                CREATE TABLE IF NOT EXISTS sign_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    sign_date TEXT NOT NULL,
                    consecutive_days INTEGER DEFAULT 1,
                    reward INTEGER DEFAULT 0,
                    sign_time TEXT NOT NULL,
                    UNIQUE(user_id, group_id, sign_date)
                )
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS user_stats (
                    user_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    user_name TEXT DEFAULT '',
                    total_days INTEGER DEFAULT 0,
                    current_consecutive INTEGER DEFAULT 0,
                    max_consecutive INTEGER DEFAULT 0,
                    total_points INTEGER DEFAULT 0,
                    last_sign_date TEXT,
                    PRIMARY KEY (user_id, group_id)
                )
            ''')
            conn.commit()

    def _get_group_key(self, event: AstrMessageEvent) -> str:
        """获取会话隔离 key，群聊用群号，私聊用 private_ 前缀"""
        gid = event.get_group_id()
        return gid if gid else f"private_{event.get_sender_id()}"

    def _check_group_only(self, event: AstrMessageEvent) -> bool:
        """检查群聊限制，返回 True 表示通过"""
        if self.config.get("仅群聊可用", True) and not event.get_group_id():
            return False
        return True

    async def _like_and_poke(self, event: AstrMessageEvent, user_id: str, times: int) -> bool:
        """给用户主页点赞并戳一戳（仅 OneBot 平台有效）

        返回 True 表示平台支持并已尝试执行；返回 False 表示非 OneBot 平台已跳过。
        点赞或戳一戳单步失败仅记录日志，不影响发情主流程。
        """
        if not isinstance(event, AiocqhttpMessageEvent):
            return False
        uid = int(user_id)
        bot = event.bot
        # 1. 主页点赞
        try:
            await bot.call_action("send_like", user_id=uid, times=times)
        except Exception as e:
            logger.warning(f"发情点赞失败(user={uid}, times={times}): {e}")
        # 2. 戳一戳：群聊用 group_poke，私聊用 friend_poke
        try:
            gid = event.get_group_id()
            if gid:
                await bot.call_action("group_poke", group_id=int(gid), user_id=uid)
            else:
                await bot.call_action("friend_poke", user_id=uid)
        except Exception as e:
            logger.warning(f"发情戳一戳失败(user={uid}): {e}")
        return True

    # ------------------------------------------------------------------
    # 指令处理
    # ------------------------------------------------------------------

    @filter.command("发情", alias={"sign", "打卡"})
    async def sign_in(self, event: AstrMessageEvent):
        """每日发情，领取随机积分奖励"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name() or user_id
        group_key = self._get_group_key(event)
        today = date.today()
        today_str = today.isoformat()

        if not self._check_group_only(event):
            yield event.plain_result("⚠️ 发情功能仅在群聊中可用哦~")
            return

        with closing(sqlite3.connect(self.db_path)) as conn:
            c = conn.cursor()

            # 检查今天是否已发情
            c.execute(
                'SELECT consecutive_days, reward FROM sign_log '
                'WHERE user_id=? AND group_id=? AND sign_date=?',
                (user_id, group_key, today_str)
            )
            existing = c.fetchone()
            if existing:
                yield event.plain_result(
                    f"⚠️ {user_name}，你今天已经发情过啦！\n"
                    f"━━━━━━━━━━━━━\n"
                    f"🔥 今日连续发情：{existing[0]} 天\n"
                    f"💰 今日获得积分：{existing[1]}\n"
                    f"明天再来吧~"
                )
                return

            # 读取用户统计
            c.execute(
                'SELECT total_days, current_consecutive, max_consecutive, '
                'total_points, last_sign_date FROM user_stats '
                'WHERE user_id=? AND group_id=?',
                (user_id, group_key)
            )
            stats = c.fetchone()
            if stats:
                total_days, current_consecutive, max_consecutive, total_points, last_sign_date_str = stats
            else:
                total_days = 0
                current_consecutive = 0
                max_consecutive = 0
                total_points = 0
                last_sign_date_str = None

            # 计算连续发情天数
            if last_sign_date_str:
                try:
                    last_date = date.fromisoformat(last_sign_date_str)
                    if last_date == today - timedelta(days=1):
                        new_consecutive = current_consecutive + 1
                    else:
                        new_consecutive = 1
                except (ValueError, TypeError):
                    new_consecutive = 1
            else:
                new_consecutive = 1

            # 计算奖励
            min_reward = self.config.get("基础奖励最小值", 10)
            max_reward = self.config.get("基础奖励最大值", 100)
            base_reward = random.randint(min_reward, max_reward)

            bonus = 0
            if self.config.get("连续发情加成", True):
                bonus_cap = self.config.get("连续发情加成上限", 30)
                effective_days = min(new_consecutive, bonus_cap)
                bonus = effective_days * 2

            total_reward = base_reward + bonus
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            # 写入发情记录
            c.execute(
                'INSERT OR REPLACE INTO sign_log '
                '(user_id, group_id, sign_date, consecutive_days, reward, sign_time) '
                'VALUES (?, ?, ?, ?, ?, ?)',
                (user_id, group_key, today_str, new_consecutive, total_reward, now_str)
            )

            # 更新统计数据
            new_total_days = total_days + 1
            new_max_consecutive = max(max_consecutive, new_consecutive)
            new_total_points = total_points + total_reward

            c.execute(
                'INSERT OR REPLACE INTO user_stats '
                '(user_id, group_id, user_name, total_days, current_consecutive, '
                'max_consecutive, total_points, last_sign_date) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (user_id, group_key, user_name, new_total_days, new_consecutive,
                 new_max_consecutive, new_total_points, today_str)
            )
            conn.commit()

        # 发情成功后：给用户主页点赞 + 戳一戳
        like_enabled = self.config.get("启用点赞与戳一戳", True)
        like_times = self.config.get("点赞次数", 10)
        liked = False
        if like_enabled:
            liked = await self._like_and_poke(event, user_id, like_times)

        # 构建回复消息
        msg = (
            f"🎉 发情成功！\n"
            f"━━━━━━━━━━━━━\n"
            f"👤 {user_name}\n"
            f"📅 {today_str}\n"
            f"━━━━━━━━━━━━━\n"
            f"💰 基础奖励：{base_reward} 积分\n"
        )
        if bonus > 0:
            msg += f"🔥 连续发情加成：+{bonus} 积分（连续{new_consecutive}天）\n"
        msg += (
            f"✨ 本次共获得：{total_reward} 积分\n"
            f"━━━━━━━━━━━━━\n"
            f"📊 累计发情：{new_total_days} 天\n"
            f"🏆 最高连续：{new_max_consecutive} 天\n"
            f"💎 总积分：{new_total_points}\n"
        )
        if new_consecutive >= 7:
            msg += f"🔥 已连续发情 {new_consecutive} 天，继续加油！\n"
        if new_consecutive == 1 and total_days > 0:
            msg += f"💡 连续发情中断了，重新开始计数~\n"
        if liked:
            msg += f"💖 今日已发情并给你{like_times}个赞~\n"

        yield event.plain_result(msg)

    @filter.command("我的发情", alias={"发情信息", "signinfo"})
    async def my_sign_info(self, event: AstrMessageEvent):
        """查询个人发情信息"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name() or user_id
        group_key = self._get_group_key(event)
        today_str = date.today().isoformat()

        if not self._check_group_only(event):
            yield event.plain_result("⚠️ 发情功能仅在群聊中可用哦~")
            return

        with closing(sqlite3.connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute(
                'SELECT user_name, total_days, current_consecutive, '
                'max_consecutive, total_points, last_sign_date '
                'FROM user_stats WHERE user_id=? AND group_id=?',
                (user_id, group_key)
            )
            stats = c.fetchone()

            if not stats:
                yield event.plain_result(
                    f"📋 {user_name}，你还没有发情记录哦~\n"
                    f"发送「发情」开始你的发情之旅吧！"
                )
                return

            db_name, total_days, current_consecutive, max_consecutive, total_points, last_sign_date_str = stats
            signed_today = (last_sign_date_str == today_str)

        status = "✅ 今日已发情" if signed_today else "❌ 今日未发情"
        msg = (
            f"📋 发情信息\n"
            f"━━━━━━━━━━━━━\n"
            f"👤 {db_name or user_name}\n"
            f"📌 状态：{status}\n"
            f"━━━━━━━━━━━━━\n"
            f"📊 累计发情：{total_days} 天\n"
            f"🔥 当前连续：{current_consecutive} 天\n"
            f"🏆 最高连续：{max_consecutive} 天\n"
            f"💎 总积分：{total_points}\n"
        )
        yield event.plain_result(msg)

    @filter.command("发情排行", alias={"发情榜单", "signrank"})
    async def sign_ranking(self, event: AstrMessageEvent):
        """查看发情积分排行榜"""
        group_key = self._get_group_key(event)
        limit = self.config.get("排行榜显示数量", 10)

        if not self._check_group_only(event):
            yield event.plain_result("⚠️ 发情功能仅在群聊中可用哦~")
            return

        with closing(sqlite3.connect(self.db_path)) as conn:
            c = conn.cursor()
            c.execute(
                'SELECT user_name, total_days, total_points, max_consecutive '
                'FROM user_stats WHERE group_id=? '
                'ORDER BY total_points DESC LIMIT ?',
                (group_key, limit)
            )
            rows = c.fetchall()

        if not rows:
            yield event.plain_result("📊 当前还没有发情排行榜，快来成为第一个发情的人吧！")
            return

        medals = ["🥇", "🥈", "🥉"]
        msg = "🏆 发情积分排行榜\n━━━━━━━━━━━━━\n"
        for i, (name, days, points, max_consec) in enumerate(rows):
            rank = i + 1
            display_name = name or f"用户{rank}"
            prefix = medals[i] if i < 3 else f"{rank}."
            msg += f"{prefix} {display_name}  💎{points}  📅{days}天  🔥{max_consec}连\n"
        msg += "━━━━━━━━━━━━━\n"
        msg += f"共 {len(rows)} 位发情用户"

        yield event.plain_result(msg)

    @filter.command("发情帮助", alias={"signhelp"})
    async def sign_help(self, event: AstrMessageEvent):
        """显示发情帮助信息"""
        msg = (
            f"📖 每日发情帮助\n"
            f"━━━━━━━━━━━━━\n"
            f"📌 发情 / sign / 打卡 — 每日发情领取积分\n"
            f"📌 我的发情 / 发情信息 — 查看个人发情信息\n"
            f"📌 发情排行 / 发情榜单 — 查看积分排行榜\n"
            f"📌 发情帮助 — 显示本帮助\n"
            f"━━━━━━━━━━━━━\n"
            f"💡 每天发情可获得随机积分，连续发情还有额外加成哦！"
        )
        yield event.plain_result(msg)

    async def terminate(self) -> None:
        """插件卸载/重载时调用"""
        logger.info("每日发情插件已卸载")
