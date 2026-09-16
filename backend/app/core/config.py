"""全局配置。

所有可调参数集中在这里，从 backend/.env 读取，代码里不出现硬编码的密钥和数值。
为什么用 pydantic-settings：它能在启动时就校验配置类型，配错了立刻报错，
而不是等到运行到某一行代码才崩。
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/ 目录（config.py 在 backend/app/core/ 下，往上三层）
BASE_DIR = Path(__file__).resolve().parent.parent.parent
# 运行期数据目录：上传的原文件、Chroma 向量库、SQLite 数据库都放这里
DATA_DIR = BASE_DIR / "data"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # .env 里有本项目不认识的变量时忽略，不报错
    )

    # ---------- 应用 ----------
    APP_NAME: str = "电商知识库问答系统"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # ---------- 数据库 ----------
    # 默认 SQLite；换成 PostgreSQL 只需改这一行，如：
    # postgresql+psycopg://user:pass@localhost:5432/ragdb
    DATABASE_URL: str = f"sqlite:///{DATA_DIR / 'app.db'}"

    # ---------- JWT 认证 ----------
    JWT_SECRET_KEY: str = "dev-only-secret-change-me"
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60  # 访问令牌 1 小时
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7  # 刷新令牌 7 天

    # ---------- 管理员种子账号 ----------
    ADMIN_USERNAME: str = "admin"
    ADMIN_PASSWORD: str = "123456"

    # ---------- 阿里云百炼 DashScope ----------
    DASHSCOPE_API_KEY: str = ""
    DASHSCOPE_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    LLM_MODEL: str = "qwen-plus"
    EMBEDDING_MODEL: str = "text-embedding-v3"
    EMBEDDING_DIM: int = 1024
    RERANK_MODEL: str = "gte-rerank-v2"

    # ---------- 跨域 ----------
    # 前端开发服务器地址，Vite 默认 5173
    CORS_ORIGINS: str = "http://localhost:5173,http://127.0.0.1:5173"

    # ---------- 文件上传 ----------
    MAX_UPLOAD_SIZE_MB: int = 50
    # 逗号分隔的允许扩展名
    ALLOWED_EXTENSIONS: str = ".pdf,.docx,.xlsx,.txt,.md"

    # 同时处理的文档数量上限。
    # **这个值不能设太大**——它是踩过坑才加的：
    # 一次性提交 17 份文档重建索引时，每个后台任务都要独占一个数据库连接，
    # 而且**在整个处理过程中（包括调云端向量化接口的那几秒）一直占着不放**。
    # 默认连接池只有 5+10=15 个连接，17 个任务一起上直接把它打满，
    # 后面的任务全部报「QueuePool limit reached」超时失败。
    # 从界面上完全看不出是连接池的问题，只表现为"文档一直卡在处理中"。
    DOCUMENT_PROCESS_CONCURRENCY: int = 2

    # ---------- 数据库连接池 ----------
    # SQLite 是文件锁模型，连接开太多反而会互相等锁，所以主要靠上面的并发限制
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    # 取不到连接时的等待时间。设短一点让它快速失败并把错误暴露出来，
    # 比让用户干等 30 秒、最后只看到"文档卡住了"要好排查得多
    DB_POOL_TIMEOUT: int = 10

    # ---------- RAG 检索参数 ----------
    CHUNK_SIZE: int = 500  # 每个分块的字符数
    CHUNK_OVERLAP: int = 80  # 相邻分块重叠字符数，避免语义被切断

    # 是否给每个分块补上「来源标题」前缀。
    # 详见 rag/splitter.py 里的说明——这是修复「分块丢失商品上下文」的关键开关，
    # 关掉可以复现修复前的效果，用于做对比实验。
    CHUNK_CONTEXT_ENABLED: bool = True
    RETRIEVE_TOP_K: int = 20  # 每路召回的候选数
    RERANK_TOP_N: int = 5  # 精排后最终送入大模型的分块数
    RERANK_SCORE_THRESHOLD: float = 0.0  # 重排分数低于此值的丢弃
    # 向量检索的相似度下限（余弦相似度，1 表示完全一致）。
    # 设得低一些比较安全：中文短句和资料长文之间，余弦相似度普遍只有 0.3~0.6，
    # 设成 0.7 这种"看起来合理"的值会把所有结果都过滤掉。
    # 宁可多召回几条让模型自己判断，也不要漏掉关键资料。
    SIMILARITY_THRESHOLD: float = 0.15
    RRF_K: int = 60  # RRF 融合公式里的平滑常数

    # 两路召回的权重。默认偏向向量（0.7/0.3）而不是等权，这是**实验测出来的**：
    # 在 30 个标注问题上，等权配置的 Recall@1 是 60%，0.7/0.3 是 66.7%。
    # 原因是 BM25 在本项目的语料上表现较弱（语料小、问题偏口语化），
    # 等权会让它和向量检索平起平坐，反而拉低了整体。
    # 语料规模变大之后 BM25 会更强，那时候可以再调回 0.5/0.5。
    RRF_VECTOR_WEIGHT: float = 0.7
    RRF_BM25_WEIGHT: float = 0.3

    # 关键词路的「置信门槛」：BM25 返回的结果少于这个数量时，
    # 说明它其实没找到真正匹配的内容（只是在勉强凑数），
    # 这时不让它参与融合。
    #
    # 为什么需要这个（实测数据）：BM25 经常只返回 1~2 条结果
    # （其余分块得分为 0 被过滤掉了）。而 RRF 只看排名不看分数，
    # 于是这 1 条的结果和向量检索的第 1 名拿到完全相同的权重，
    # 一个不靠谱的结果就这样和靠谱的排在了并列位置，
    # 谁在前面取决于字典插入顺序——等于引入了随机噪声。
    #
    # 加门槛后的实测效果（30 题）：Recall@1 从 60% 提升到 66.7%，MRR 从 0.699 到 0.758。
    BM25_MIN_RESULTS: int = 5

    # ---------- 检索策略开关 ----------
    # 这几个开关是为了做「效果对比实验」而存在的：
    # scripts/eval_rag.py 会依次打开不同的组合，跑同一批问题，
    # 对比准确率差异，用于论文的实验章节和答辩时的效果演示。
    #
    # RETRIEVAL_MODE 取值：
    #   vector  —— 只用向量检索（最基础的方案，作为对照基线）
    #   keyword —— 只用关键词检索
    #   hybrid  —— 两路混合 + RRF 融合（推荐）
    RETRIEVAL_MODE: str = "hybrid"
    RERANK_ENABLED: bool = True  # 是否启用重排
    QUERY_REWRITE_ENABLED: bool = True  # 是否启用历史感知查询改写
    HISTORY_MAX_TURNS: int = 6  # 送入大模型的历史对话轮数上限（token 预算控制）

    # ---------- 缓存 ----------
    CACHE_TTL_SECONDS: int = 3600  # 答案缓存存活时间
    CACHE_MAX_SIZE: int = 1000  # 答案缓存最大条目数

    # ---------- 统计 ----------
    # 统计看板按「天」分组时用的时区偏移（小时），默认东八区。
    #
    # 为什么需要这个：数据库里的时间统一存 UTC（见 utils/time.py），
    # 但看板的"今天"必须按用户所在时区来算。如果不做转换，
    # 北京时间早上 7 点提问会被算成"昨天"，而"今日问答"要到早上 8 点才刷新——
    # 数字看着就是错的，而且很难联想到是时区问题。
    #
    # 为什么用固定偏移而不是完整时区库：中国全境统一用北京时间、不实行夏令时，
    # 全年固定 +8，用数字最简单也最不容易出错。
    # 如果将来要部署到有夏令时的地区，这里得换成 zoneinfo。
    STATS_TIMEZONE_OFFSET_HOURS: int = 8

    # ---------- 限流 ----------
    RATE_LIMIT_PER_MINUTE: int = 60  # 每用户每分钟请求上限
    CHAT_CONCURRENCY_PER_USER: int = 3  # 单用户同时进行的问答数上限

    # ---------- 派生属性 ----------
    @property
    def cors_origin_list(self) -> list[str]:
        """把逗号分隔的跨域配置转成列表。"""
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def allowed_extension_set(self) -> set[str]:
        """把逗号分隔的扩展名配置转成小写集合，方便判断。"""
        return {e.strip().lower() for e in self.ALLOWED_EXTENSIONS.split(",") if e.strip()}

    @property
    def upload_dir(self) -> Path:
        """上传原文件的存放目录。"""
        return DATA_DIR / "uploads"

    @property
    def chroma_dir(self) -> Path:
        """Chroma 向量库的持久化目录。"""
        return DATA_DIR / "chroma_db"

    def ensure_dirs(self) -> None:
        """确保运行期需要的目录都存在（首次启动时自动创建）。"""
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.chroma_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例。

    用 lru_cache 保证整个进程只解析一次 .env，避免每次调用都读磁盘。
    """
    return Settings()


settings = get_settings()
