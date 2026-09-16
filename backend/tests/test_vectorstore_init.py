"""向量库「首次初始化」的并发安全测试。

这个文件专门守护一类**只在冷启动时才会出现**的 bug，它们有两个共同特点：
  1. 单元测试很难发现——因为测试夹具通常已经预先把客户端设好了，
     走的都是"已经初始化过"的快路径，压根碰不到出问题的分支
  2. 一旦发生，表现是"卡死"或"第一次提问必失败"，看起来跟功能本身毫无关系

所以这里**故意把全局状态清空**，模拟服务刚启动、什么都还没建的那一刻。
"""

import threading

from langchain_core.embeddings import Embeddings

from app.rag import vectorstore


class FakeEmbeddings(Embeddings):
    """假的向量化函数，不调用任何外部接口。"""

    def _vector(self, text: str) -> list[float]:
        return [0.1] * 8

    def embed_documents(self, texts):
        return [self._vector(t) for t in texts]

    def embed_query(self, text):
        return self._vector(text)


def _run_with_timeout(func, timeout=20):
    """在单独的线程里跑 func，超时就判定失败。

    为什么要这么麻烦：要防的那个 bug 是**死锁**，
    死锁的表现是"永远不返回"，用普通的 assert 根本测不出来——
    整个测试进程会一直挂在那里，CI 上表现为超时而不是一条失败的用例，
    排查时完全看不出是哪里出的问题。
    所以这里主动设一个超时，把"卡住"转成一条明确的失败。
    """
    result = {}

    def target():
        try:
            result["value"] = func()
        except Exception as e:  # noqa: BLE001 - 测试里要把任何异常都记下来
            result["error"] = e

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(timeout=timeout)

    assert not thread.is_alive(), (
        f"初始化在 {timeout} 秒内没有完成，基本可以确定是死锁了。"
        "检查 get_store 有没有在持有 _init_lock 的情况下调用 get_client"
        "（threading.Lock 不可重入，同一个线程二次抢锁会永远等下去）"
    )
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _reset_global_state(monkeypatch, tmp_path):
    """把向量库的全局缓存清空，回到"服务刚启动"的状态。

    注意用 monkeypatch 而不是直接改模块变量：
    monkeypatch 会在测试结束后自动还原，不会污染后面的测试。

    **锁也要换成新的**，这一点很关键：万一某个用例真的触发了死锁，
    那个卡住的守护线程会**永远**占着旧的锁不放。如果后面的用例还用同一把锁，
    它们会全部跟着挂起，整个测试进程就收不了尾了——
    表现是"跑测试卡死"，而不是"有几条用例失败"，排查时完全看不出所以然。
    每个用例换一把新锁，一个用例挂住不影响其他用例。
    """
    monkeypatch.setattr(vectorstore, "_client", None)
    monkeypatch.setattr(vectorstore, "_stores", {})
    monkeypatch.setattr(vectorstore, "_init_lock", threading.Lock())

    # 重新指定一个临时目录，避免真的去打开项目的数据目录
    from app.core.config import settings

    monkeypatch.setattr(
        type(settings), "chroma_dir", property(lambda self: tmp_path / "chroma_db")
    )


def test_cold_start_does_not_deadlock(monkeypatch, tmp_path):
    """冷启动时创建客户端和 collection，不能死锁。

    这是真实踩到过的 bug：修并发竞争时给 get_store 加了锁，
    但锁里面又调了同样要抢这把锁的 get_client。
    threading.Lock 不可重入，于是第一次提问直接卡死——
    接口既不返回也不报错，日志里一片安静。

    188 个单元测试全部通过，因为它们用的夹具预先设好了 _client，
    get_client 走的是不抢锁的快路径，根本进不到死锁分支。
    """
    _reset_global_state(monkeypatch, tmp_path)
    monkeypatch.setattr(vectorstore, "get_embeddings", FakeEmbeddings)

    store = _run_with_timeout(lambda: vectorstore.get_store(1))
    assert store is not None


def test_concurrent_first_access_creates_one_client(monkeypatch, tmp_path):
    """多个线程同时第一次访问，只能建出一个客户端。

    不加锁的话，每个线程都会发现"客户端还没建"，于是各建一个
    指向**同一个目录**的 Chroma 客户端。Chroma 内部按路径管单例，
    第二个一建出来就把第一个的 System 停掉，
    正在用它的那个线程随即拿到失效句柄，报出
    `'RustBindingsAPI' object has no attribute 'bindings'` 这种
    和业务毫无关系的错误。

    这个场景在真实运行中很容易触发：检索是两路并行的
    （向量路 + 跑在线程池里的关键词路），而"第一次用到某个知识库"
    往往正好就是两路同时开工的那一刻。
    """
    _reset_global_state(monkeypatch, tmp_path)
    monkeypatch.setattr(vectorstore, "get_embeddings", FakeEmbeddings)

    THREADS = 8
    # 用栅栏让 8 个线程尽可能在同一瞬间冲进去。
    # 不设栅栏的话线程是依次启动的，第一个可能已经把初始化做完了，
    # 后面的全走快路径，等于没测到并发
    barrier = threading.Barrier(THREADS)
    stores: list = [None] * THREADS

    def worker(index):
        barrier.wait()
        stores[index] = vectorstore.get_store(1)

    def run_concurrently():
        # **用裸的守护线程，不用 ThreadPoolExecutor**。
        # 原因是 ThreadPoolExecutor 注册了 atexit 钩子，退出解释器时
        # 会去 join 所有工作线程。万一某个线程死锁了，测试虽然能跑完
        # 并报出失败，进程却卡在退出阶段永远结束不了——
        # 上层只能看到"超时"，看不到那几条已经报出来的失败。
        # 守护线程不会被 join，进程该退就退。
        threads = [
            threading.Thread(target=worker, args=(i,), daemon=True)
            for i in range(THREADS)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        return stores

    # 同样要包超时：死锁时这里的线程会一直等下去，
    # 不设超时的话整个测试进程会挂住（表现为 CI 超时，而不是一条失败的用例）
    stores = _run_with_timeout(run_concurrently)

    # 所有线程拿到的必须是**同一个对象**。拿到不同对象就说明
    # 客户端被重复创建了，那条路径上迟早会出 RustBindingsAPI 错误
    assert len({id(s) for s in stores}) == 1
    assert len(vectorstore._stores) == 1


def test_different_kbs_get_different_stores(monkeypatch, tmp_path):
    """不同的知识库要拿到不同的 collection（数据隔离的基础）。"""
    _reset_global_state(monkeypatch, tmp_path)
    monkeypatch.setattr(vectorstore, "get_embeddings", FakeEmbeddings)

    # 每个可能死锁的调用都要包超时。理由：这几个用例都是"冷启动"场景，
    # 一旦 get_store 里再出现嵌套抢锁，它们**每一个**都会卡住。
    # 漏掉任何一个，整个测试进程就会挂在那里收不了尾——
    # 表现成"跑测试卡死"而不是"用例失败"，非常难排查
    store1 = _run_with_timeout(lambda: vectorstore.get_store(1))
    store2 = _run_with_timeout(lambda: vectorstore.get_store(2))

    assert store1 is not store2
    assert store1._collection.name == "kb_1"
    assert store2._collection.name == "kb_2"


def test_forget_store_forces_rebuild(monkeypatch, tmp_path):
    """丢掉缓存后重新取，应该是一个新的对象（自愈机制的基础）。"""
    _reset_global_state(monkeypatch, tmp_path)
    monkeypatch.setattr(vectorstore, "get_embeddings", FakeEmbeddings)

    first = _run_with_timeout(lambda: vectorstore.get_store(1))
    vectorstore._forget_store(1)
    second = _run_with_timeout(lambda: vectorstore.get_store(1))

    assert first is not second
    # 但指向的还是同一个 collection，数据不会丢
    assert first._collection.name == second._collection.name
