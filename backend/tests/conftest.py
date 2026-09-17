"""pytest 公共夹具。

核心思路：测试时把数据库换成「内存版 SQLite」，好处是：
  - 每次测试都是全新的干净数据库，测试之间互不干扰
  - 测试跑完数据自动消失，不会污染你真实的 app.db
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base, get_db
from app.main import app
from app.rag import pipeline
from app.services import document_service

# 导入所有模型，确保建表时不会漏表
from app.models import *  # noqa: F401,F403

# SQLite 内存库。StaticPool 让所有连接共用同一个内存数据库，
# 否则每次新建连接都会得到一个空的新库，表就"不见了"
test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(
    bind=test_engine, autoflush=False, expire_on_commit=False
)


@pytest.fixture(autouse=True)
def isolated_vectorstore(tmp_path, monkeypatch):
    """把向量库重定向到临时目录，并清空缓存。

    **这个夹具非常关键，不是可选项。**

    背景：测试用的关系数据库是内存库（见上面的 test_engine），
    但向量库 Chroma 是直接读写磁盘文件的。如果不做隔离，就会出事：

        测试里删除「知识库 1」→ 真的调用 drop_kb(1)
        → 把磁盘上真实知识库 1 的向量全部删掉

    测试用的知识库 ID 总是从 1 开始，和真实数据的 ID 完全重合，
    所以跑一遍测试就等于把真实向量库清空一遍。
    （这不是假想——实际开发中踩过，真实数据被测试删掉过。）

    做法：给每个测试新建一个指向临时目录的 Chroma 客户端，
    并清空模块级的缓存字典，测试结束后 monkeypatch 会自动还原。
    """
    import chromadb

    from app.rag import vectorstore

    client = chromadb.PersistentClient(path=str(tmp_path / "chroma_db"))
    monkeypatch.setattr(vectorstore, "_client", client)
    monkeypatch.setattr(vectorstore, "_stores", {})

    yield


@pytest.fixture(autouse=True)
def isolated_upload_dir(tmp_path, monkeypatch):
    """把上传目录也重定向到临时目录。

    **和上面的向量库隔离是同一类问题**：上传接口会把文件真的写到
    `backend/data/uploads/` 下，而测试用的知识库 ID 总是从 1 开始，
    和真实数据的 ID 完全重合。

    不隔离的后果实测过：真实的上传目录里堆了 **227 个文件，
    而数据库只引用了其中 17 个**——其余全是测试跑出来的，
    以及历史遗留没人清理的。文件本身不大（几百 KB），
    但"目录里的文件和库里的记录对不上"会让排查问题的人非常困惑。

    做法：把 settings.upload_dir 换成临时目录。它是 property，
    所以要在类上替换。
    """
    from app.core.config import settings

    monkeypatch.setattr(
        type(settings), "upload_dir", property(lambda self: tmp_path / "uploads")
    )
    (tmp_path / "uploads").mkdir(parents=True, exist_ok=True)

    yield


@pytest.fixture(autouse=True)
def reset_global_state():
    """清空跨测试共享的全局状态。

    有三个东西是进程级单例，测试之间会互相污染：

      1. **限流器**：测试库是每次重建的，用户 ID 总是从 1 开始。
         上一个测试对 user:1 发了 20 次请求，下一个测试里 user:1
         可能就被限流了——报出来的错跟被测功能毫无关系，很误导。
      2. **问答缓存**：缓存键只包含问题和知识库 ID，不含用户。
         测试 A 问「电池多大」缓存了答案，测试 B 里同样的问题
         会直接命中缓存、根本不走被测代码，测试就形同虚设了。
      3. **向量化缓存**：同理，会让"应该调接口"的测试不调接口。
      4. **缓存的命中/未命中计数器**：统计看板要断言"命中率是多少"。
         计数器是累加的，上一个测试的命中会算进这一个测试里。
         注意 invalidate_answers() **只清数据、不清计数器**，
         要清计数器得单独调 reset_cache_stats()——
         这两件事分开是有意的：线上清缓存时不该把历史命中率一起抹掉。
    """
    from app.core import cache as answer_cache
    from app.core import rate_limit
    from app.rag.embeddings import get_embeddings

    answer_cache.invalidate_answers()
    answer_cache.reset_cache_stats()
    rate_limit.api_limiter.reset()
    rate_limit.chat_limiter.reset()

    # 向量化缓存放在实例里，直接清掉它的内部字典
    get_embeddings()._cache.clear()

    yield

    answer_cache.invalidate_answers()
    answer_cache.reset_cache_stats()
    rate_limit.api_limiter.reset()
    rate_limit.chat_limiter.reset()


@pytest.fixture
def db_session():
    """每个测试用例一个干净的数据库。"""
    Base.metadata.create_all(bind=test_engine)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=test_engine)


@pytest.fixture
def client(db_session):
    """测试用的 HTTP 客户端，数据库已被替换成内存库。

    注意这里刻意不用 `with TestClient(app)`：
    用 with 会触发 FastAPI 的 lifespan，也就是会跑到真实的 init_db()，
    在真实的 app.db 里建表并创建管理员账号。测试不应该有这种副作用。
    """
    def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def registered_user(client):
    """预先注册好一个普通用户，返回 (用户名, 密码)。"""
    client.post("/api/v1/auth/register", json={"username": "tester", "password": "test123456"})
    return "tester", "test123456"


@pytest.fixture
def auth_headers(client, registered_user):
    """已登录用户的请求头，直接塞进请求就能通过鉴权。"""
    username, password = registered_user
    resp = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def admin_headers(client, db_session):
    """管理员账号的请求头。

    注意：不走真实的 seed_admin()，而是直接在测试库里插一个管理员。
    因为 seed_admin 是启动流程的一部分，测试里单独调用它会让
    「测试数据」和「启动逻辑」耦合在一起，将来改了启动逻辑测试就会莫名其妙挂掉。
    """
    from app.core.security import hash_password
    from app.models.user import User

    admin = User(
        username="admin_for_test",
        password_hash=hash_password("admin123456"),
        role="admin",
    )
    db_session.add(admin)
    db_session.commit()

    resp = client.post(
        "/api/v1/auth/login", json={"username": "admin_for_test", "password": "admin123456"}
    )
    token = resp.json()["data"]["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def sample_kb(client, admin_headers):
    """预先建好一个知识库，返回它的信息字典。"""
    resp = client.post(
        "/api/v1/knowledge-bases",
        headers=admin_headers,
        json={"name": "测试知识库", "description": "单元测试用"},
    )
    return resp.json()["data"]


@pytest.fixture
def fake_pipeline(monkeypatch):
    """把真实的 RAG 流水线换成假的，记录下所有调用参数供断言。

    **为什么必须替换**：真实流水线会调用百炼接口做向量化和生成，
    既慢（每次好几秒）又要花钱。单元测试不该依赖外部服务，也不该产生费用。

    返回的 calls 列表里存着每次调用的入参，
    测试可以断言"历史对话有没有正确传给流水线"这类逻辑。

    放在 conftest 而不是各个测试文件里：问答、反馈、统计三组接口的测试
    都需要它，抄三遍的话以后改流水线签名就要改三个地方，很容易漏。
    """
    calls = []

    # 参数要和 pipeline.answer 的真实签名保持一致。
    # 少写一个参数的话，真实调用传进来就会报 TypeError——
    # 这个错误只在走完整流程的测试里才会暴露，很容易漏
    async def _fake_answer(question, history, kb_ids, kb_summary=""):
        calls.append(
            {
                "question": question,
                "history": history,
                "kb_ids": kb_ids,
                "kb_summary": kb_summary,
            }
        )

        return pipeline.ChatResult(
            answer=f"关于「{question}」，资料显示是这样的 [1]。",
            citations=[
                {
                    "index": 1,
                    "chunk_id": "doc1_c0",
                    "doc_id": 1,
                    "filename": "产品手册.md",
                    "page": 3,
                    "source_label": "产品手册.md 第 3 页",
                    "score": 0.8123,
                    "text": "这是被引用的原文片段。",
                    "truncated": False,
                    "used": True,
                }
            ],
            retrieved_query=question,
            prompt_tokens=800,
            completion_tokens=50,
            latency_ms=1234,
            stage_latency={"rewrite": 0, "retrieve": 300, "first_token": 280, "generate": 900},
        )

    monkeypatch.setattr(pipeline, "answer", _fake_answer)
    return calls


class FakeVectorStore:
    """假的向量库，记录所有写入操作，供断言检查。

    为什么要假造一个：真实的向量库调用会触发云端向量化接口，
    每次几秒还要花钱。而测试要验证的是"流程有没有走对"，
    不是"Chroma 能不能存向量"——后者是官方库自己的事，不该由我们的测试买单。

    放在 conftest 里是因为「文档处理流程」和「文档生命周期」两组测试都要用。
    """

    def __init__(self):
        # kb_id → {doc_id: [分块内容]}
        self.stored: dict[int, dict[int, list[str]]] = {}
        self.add_calls = 0
        self.delete_calls = 0

    async def add_chunks(self, kb_id: int, docs, start_index: int = 0):
        self.add_calls += 1
        doc_id = docs[0].metadata["doc_id"] if docs else 0
        self.stored.setdefault(kb_id, {}).setdefault(doc_id, [])
        self.stored[kb_id][doc_id].extend(d.page_content for d in docs)
        return len(docs)

    async def delete_by_doc(self, kb_id: int, doc_id: int):
        self.delete_calls += 1
        self.stored.get(kb_id, {}).pop(doc_id, None)

    async def drop_kb(self, kb_id: int):
        self.stored.pop(kb_id, None)

    async def search_async(self, kb_id, query, k=5):
        return []

    async def search_multi(self, kb_ids, query, k=5):
        return []


@pytest.fixture
def fake_store(monkeypatch):
    """把 document_service 用到的向量库函数全换成假的。"""
    from app.rag import vectorstore

    store = FakeVectorStore()
    monkeypatch.setattr(document_service.vectorstore, "add_chunks", store.add_chunks)
    monkeypatch.setattr(document_service.vectorstore, "delete_by_doc", store.delete_by_doc)
    monkeypatch.setattr(document_service.vectorstore, "drop_kb", store.drop_kb)
    monkeypatch.setattr(vectorstore, "add_chunks", store.add_chunks)
    monkeypatch.setattr(vectorstore, "delete_by_doc", store.delete_by_doc)
    monkeypatch.setattr(vectorstore, "drop_kb", store.drop_kb)
    return store


@pytest.fixture
def patched_session(monkeypatch, db_session):
    """让 process_document 用测试数据库的会话。

    为什么需要：process_document 跑在后台，它必须自己开数据库会话
    （请求用的那个会话在接口返回时就关了）。测试里要让它开出来的会话
    指向测试用的内存库，否则会去连真实的 app.db。
    """
    import app.services.document_service as ds
    from tests.conftest import TestingSessionLocal

    monkeypatch.setattr(ds, "SessionLocal", TestingSessionLocal)
    return db_session


@pytest.fixture
def real_process(monkeypatch):
    """把上传接口触发的后台处理换成空操作，并返回**真正的处理函数**。

    为什么需要替换：上传接口会在后台真的执行「解析→分块→调百炼接口向量化」，
    那一步既慢（每次几秒）又要花钱（真的在调用云端接口）。
    只验证接口逻辑的测试不该依赖外部服务、也不该产生费用。

    为什么要把原函数返回出去：替换是直接改模块属性的，
    测试里如果还写 `document_service.process_document(...)`，
    调到的就是那个空操作、什么都不会发生（这正是踩过的坑）。
    所以这里显式把原函数交出去，需要跑真实流程的测试用返回值调用：

        async def test_xxx(real_process):
            await real_process(doc_id)   # 这才是真的处理
    """
    from app.services import document_service

    original = document_service.process_document

    async def _noop(doc_id: int) -> None:
        return None

    monkeypatch.setattr(document_service, "process_document", _noop)
    return original
