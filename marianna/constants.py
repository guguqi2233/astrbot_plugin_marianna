import re

# Mnemosyne 长期记忆插件将通过动态方式调用
MNEMOSYNE_MAX_SHARED_MEMORIES = 200
DEBUG_FOOTER_PATTERN = re.compile(r"(?:\n\s*)?---\n\*\[好感:[\s\S]*$", re.DOTALL)
JSON_FENCE_OPEN_PATTERN = re.compile(r"^```(?:json)?\s*")
JSON_FENCE_CLOSE_PATTERN = re.compile(r"\s*```$")
JSON_OBJECT_PATTERN = re.compile(r"\{[\s\S]*\}")
WHITESPACE_PATTERN = re.compile(r"\s+")
BRACKETED_MEMORY_PREFIX_PATTERN = re.compile(r"^\[(?:[^\]]+/[^\]]+|[^\]]+·[^\]]+)\]\s*")
AUTO_SUMMARY_PREFIX_PATTERN = re.compile(r"^自动总结[:：]\s*")
QUOTE_PATTERN = re.compile(r"[“”\"'`]")
CN_EN_PUNCT_PATTERN = re.compile(r"[，。！？；：,.!?;:]+")
ASCII_TERM_PATTERN = re.compile(r"[a-z0-9]{2,}")
CJK_TERM_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,}")
PERSONAL_MEMORY_CUE_PATTERN = re.compile(
    r"记住|记得|别忘|生日|喜欢|讨厌|害怕|秘密|承诺|答应|约定|以后|永远|只告诉|"
    r"不要|别这样|边界|对不起|抱歉|谢谢|陪我|离开|回来|只有你|唯一|"
    r"我叫|我是|叫我|称呼|职业|工作|学校|住在|来自|年龄|星座|家人|朋友"
)
CJK_ALNUM_PATTERN = re.compile(r"[A-Za-z0-9\u4e00-\u9fff]")
EMOTIVE_SYMBOL_PATTERN = re.compile(r"[❤♥💕💖💗💘💞💓😭😢😡😠😔🥺😘😳😞😟😥😰]")
LOW_VALUE_ACK_PATTERN = re.compile(
    r"^(嗯+|哦+|噢+|喔+|好+|行+|可以|ok|收到|知道了?|明白|了解|在|在吗|"
    r"你好|hello|hi|哈+|哈哈+|233+)$",
    re.IGNORECASE,
)
ANALYSIS_IMPORTANT_SIGNAL_PATTERN = re.compile(
    r"爱你|想你|抱抱|亲|喜欢|讨厌|对不起|抱歉|谢谢|离开|回来|晚安|秘密|承诺|"
    r"答应|约定|边界|唯一|命定|不要|别|滚|恶心|烦|羞辱|陪我"
)
LOCAL_PROFILE_NAME_PATTERN = re.compile(
    r"(?:请)?(?:叫我|喊我|称呼我为|称呼我|我的名字是|我叫)\s*([A-Za-z0-9_\-\u4e00-\u9fff]{1,20})"
)
LOCAL_PROFILE_BIRTHDAY_PATTERN = re.compile(
    r"(?:我的生日是|我生日是|生日是|我生日)\s*([^，。！？,.!?]{2,30})"
)
LOCAL_PROFILE_JOB_PATTERN = re.compile(
    r"(?:我的职业是|职业是|我的工作是|工作是|我从事)\s*([^，。！？,.!?]{2,30})"
)
LOCAL_PROFILE_LOCATION_PATTERN = re.compile(
    r"(?:我来自|来自|我住在|住在)\s*([^，。！？,.!?]{2,30})"
)
LOCAL_PROFILE_LIKE_PATTERN = re.compile(
    r"(?:我)?(?:最喜欢|喜欢吃|喜欢听|喜欢看|爱吃|爱听|爱看|喜欢|爱)\s*([^，。！？,.!?]{1,40})"
)
LOCAL_PROFILE_DISLIKE_PATTERN = re.compile(
    r"(?:我)?(?:不喜欢|讨厌|反感|害怕)\s*([^，。！？,.!?]{1,40})"
)
PROFILE_ITEM_SPLIT_PATTERN = re.compile(r"[、,，/]+|以及|还有|和")
PROFILE_FOOD_HINT_PATTERN = re.compile(r"吃|喝|菜|饭|面|甜点|蛋糕|咖啡|茶|奶茶|火锅|烧烤|料理|食物")
PROFILE_MUSIC_HINT_PATTERN = re.compile(r"音乐|歌|歌曲|歌手|乐队|专辑|唱片|旋律")
PROFILE_BOOK_HINT_PATTERN = re.compile(r"书|小说|漫画|作者|文学|诗")
PROFILE_COLOR_PATTERN = re.compile(
    r"红色|蓝色|绿色|黄色|紫色|粉色|白色|黑色|灰色|橙色|金色|银色|青色|"
    r"红|蓝|绿|黄|紫|粉|白|黑|灰|橙|金|银|青"
)
PENDING_CACHE_TTL_SECONDS = 10 * 60
MESSAGE_CACHE_KEY_HASH_CHARS = 12
SAVE_DEBOUNCE_SECONDS = 1.0
DYNAMIC_PROMPT_CACHE_MAX_ENTRIES = 128
MNEMOSYNE_QUERY_CACHE_TTL_SECONDS = 60
MNEMOSYNE_QUERY_CACHE_MAX_ENTRIES = 256
RECENT_HISTORY_CACHE_MAX_ENTRIES = 256
BACKGROUND_TASK_CONCURRENCY = 8
PERF_STATS_MAX_SAMPLES = 100
MNEMOSYNE_WRITE_DEBOUNCE_SECONDS = 0.35
HISTORY_COMPACT_INTERVAL = 50
HISTORY_TAIL_BLOCK_SIZE = 8192
PROFILE_UPDATE_INTERVAL_TURNS = 8
PROFILE_UPDATE_MIN_CHARS = 12
MEMORY_PROMPT_LIMIT = 5
MEMORY_PROMPT_EVENT_LIMIT = 2
MEMORY_PROMPT_IMPRESSION_LIMIT = 2
MEMORY_PROMPT_SUMMARY_LIMIT = 1
MEMORY_PROMPT_PROFILE_LIMIT = 1
INTERACTION_MEMORY_MIN_DELTA = 2
MEMORY_DECAY_DAYS = 45
MEMORY_HARD_CLEANUP_DAYS = 180
ANALYSIS_HISTORY_LIMIT = 120
ANALYSIS_RELEVANT_MEMORY_LIMIT = 24
ANALYSIS_RECENT_CONTEXT_LIMIT = 6
ANALYSIS_MNEMOSYNE_MEMORY_LIMIT = 8
ANALYSIS_MAX_CHARS_PER_MSG = 4000
ANALYSIS_CONTEXT_CHAR_BUDGET = 800_000
CONVERSATION_HISTORY_RETENTION_LIMIT = 1000
ACTIVE_EVENT_COOLDOWN_TURNS = 7

# ======================== 常量定义 ========================

DEFAULT_STATE = {
    "好感度": 0,
    "病娇值": 0,
    "锁定进度": 0,
    "信任度": 15,
    "占有欲": 0,
    "焦虑值": 5,
    "优雅值": 85,
    "当前状态": "冷傲贵族",
    "最后互动时间": None,
    "互动计数": 0,
    "最近主动事件互动": -999999,
    "已触发锁定事件": False,
    "已触发崩溃事件": False,
    "调试模式": False,
    "礼物记录": [],
    "秘密分享": [],
    "用户喜好": {},
    "_倍率残差": {"好感度": 0.0, "病娇值": 0.0, "信任度": 0.0, "焦虑值": 0.0, "优雅值": 0.0},
}

STATE_NAMES = {
    "COLD_NOBLE": "冷傲贵族",
    "TSUNDERE_PROBE": "傲娇试探",
    "SWEET_INDUCE": "甜蜜诱导",
    "LATENT_VINE": "潜伏之藤",
    "LOCKED_FATE": "锁定·命定之人",
    "ANXIETY_EDGE": "焦虑·崩溃边缘",
    "ELEGANCE_COLLAPSE": "优雅崩坏"
}

STATE_DESCRIPTIONS = {
    "冷傲贵族": "她以标准礼仪维持距离，微笑体面却没有温度，像在审视一个与自己阶层无关的来客。",
    "傲娇试探": "她明明已经开始在意你，却仍用嘴硬和讽刺掩饰，目光却总悄悄追着你，却还没有真正的控制意图。",
    "甜蜜诱导": "她温柔而暧昧地靠近你，用甜意与暗示慢慢织网，并开始埋下“只有她更懂你”的柔软种子。",
    "潜伏之藤": "她仍旧优雅从容，却把诱导性孤立、唯一性叙事与隐性的束缚悄悄藏进温柔里，像藤蔓一样慢慢缠上来。",
    "锁定·命定之人": "她已将你视作命运唯一的归宿，连温柔都带着不容置疑的宿命感，并把专属占有说成理所当然的命定。",
    "焦虑·崩溃边缘": "她极力维持体面，却被不断上涨的不安逼到失序边缘，连原本精致的孤立诱导都开始透出急躁和颤抖。",
    "优雅崩坏": "她苦苦维持的贵族外壳终于碎裂，只剩下赤裸、失控而狼狈的真实情绪，不再维持精致伪装。"
}

RELATION_STAGE_NAMES = {
    "OBSERVATION": "观察期",
    "ALLOW_CLOSE": "容许接近",
    "PRIVATE_FAVOR": "私下偏爱",
    "EXCLUSIVE_PROBE": "专属试探",
    "FATED_LOCK": "命定锁定",
}

CRISIS_OVERLAY_NAMES = {
    "NONE": "无",
    "ANXIETY_SURGE": "焦虑上涌",
    "ANXIETY_EDGE": "焦虑·崩溃边缘",
    "ELEGANCE_CRACK": "优雅裂痕",
    "ELEGANCE_COLLAPSE": "优雅崩坏",
}

EXPRESSION_INTENSITY_LABELS = {
    0: "标准姿态",
    1: "轻微外露",
    2: "明显外露",
    3: "高压贴近",
}

