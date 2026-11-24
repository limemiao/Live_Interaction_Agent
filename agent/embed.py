"""本地中文向量检索：把历史内容索引起来，支持语义检索。

为什么必须换掉原来的全文检索：
    `search_text(text[:8])` 拿口语问句的前 8 个字去查 FTS 索引，
    几乎不可能命中任何历史内容——实测一次都没返回过结果。
    口语检索必须靠语义相似度，不是字面匹配。

模型用 bge-small-zh-v1.5，从 Hugging Face 缓存离线加载，不产生网络调用或费用。
"""

from pathlib import Path

import numpy as np

from .model_paths import EMBED_BGE_SMALL_ZH, cached_snapshot

# bge 官方建议：检索用的 query 加此前缀，文档侧不加
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class Embedder:
    def __init__(self, model_dir=None, device=None, max_length=128):
        import torch
        from transformers import AutoModel, AutoTokenizer

        self.dir = str(model_dir or cached_snapshot(EMBED_BGE_SMALL_ZH))
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.torch = torch
        self.tok = AutoTokenizer.from_pretrained(self.dir)
        self.model = AutoModel.from_pretrained(self.dir).to(self.device).eval()
        self.max_length = max_length
        self.dim = self.model.config.hidden_size

    def encode(self, texts, batch_size=32, is_query=False):
        """返回 L2 归一化后的向量矩阵 (n, dim)。"""
        torch = self.torch
        if is_query:
            texts = [QUERY_PREFIX + t for t in texts]
        out = []
        for i in range(0, len(texts), batch_size):
            batch = texts[i:i + batch_size]
            enc = self.tok(batch, padding=True, truncation=True,
                           max_length=self.max_length, return_tensors="pt")
            enc = {k: v.to(self.device) for k, v in enc.items()}
            with torch.no_grad():
                last = self.model(**enc).last_hidden_state
            mask = enc["attention_mask"].unsqueeze(-1).float()
            vec = (last * mask).sum(1) / mask.sum(1).clamp(min=1e-6)
            vec = torch.nn.functional.normalize(vec, p=2, dim=1)
            out.append(vec.cpu().numpy().astype(np.float32))
        if not out:
            return np.zeros((0, self.dim), dtype=np.float32)
        return np.vstack(out)


class VectorIndex:
    """向量存 SQLite，检索时整表载入内存算余弦。

    数据量是万级（512 维 float32 一条 2KB，一万条才 20MB），
    暴力检索完全够用，不需要引入专门的向量数据库。
    """

    SELECT_PENDING = (
        "SELECT e.id, e.text_norm FROM events e "
        "WHERE e.type=? AND e.text_norm IS NOT NULL "
        "AND e.is_noise=0 "
        "AND length(e.text_norm)>=2 "
        "AND NOT EXISTS (SELECT 1 FROM vectors v WHERE v.kind=? AND v.ref_id=e.id)"
    )

    def __init__(self, store, embedder):
        self.store = store
        self.emb = embedder
        self._cache = None
        self._count = -1

    def build(self, kinds=("speech", "danmaku"), batch_size=64, verbose=True,
              limit=None):
        """把还没建索引的事件补上，返回新增条数。

        limit 用来限制单次最多索引多少条：实时服务里定时补索引时，
        积压很多也不该让这一步长时间占着（下一轮会接着补）。
        """
        conn = self.store.conn
        added = 0
        for kind in kinds:
            rows = conn.execute(self.SELECT_PENDING, (kind, kind)).fetchall()
            if limit is not None:
                rows = rows[:max(0, limit - added)]
            if verbose:
                print("  %s: 待索引 %d 条" % (kind, len(rows)))
            for i in range(0, len(rows), batch_size):
                chunk = rows[i:i + batch_size]
                texts = [r[1] for r in chunk]
                vecs = self.emb.encode(texts, is_query=False)
                for (rid, _), v in zip(chunk, vecs):
                    conn.execute(
                        "INSERT INTO vectors(kind, ref_id, text, vec) "
                        "VALUES (?,?,?,?)",
                        (kind, rid, "",
                         v.astype(np.float32).tobytes()))
                added += len(chunk)
                if verbose and added % 512 < batch_size:
                    print("    已索引 %d 条 ..." % added)
            if limit is not None and added >= limit:
                break
        conn.commit()
        self._cache = None
        return added

    def _load(self):
        conn = self.store.conn
        n = conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        if self._cache is not None and self._count == n:
            return self._cache
        rows = conn.execute(
            "SELECT id, kind, ref_id, vec FROM vectors").fetchall()
        if not rows:
            self._cache = (np.zeros((0, self.emb.dim), np.float32), [], [])
            self._count = 0
            return self._cache
        mat = np.vstack([np.frombuffer(r[3], dtype=np.float32) for r in rows])
        self._cache = (mat, [r[1] for r in rows], [r[2] for r in rows])
        self._count = n
        return self._cache

    def search(self, query, top_k=6, kinds=None, exclude_session=None,
               min_score=0.0, dedup=True):
        """语义检索，返回 [{kind, ref_id, text, uname, ts, score}]

        min_score 是相似度下限：低于它就当作"没有相关历史"。
        没有下限的话，无关时也会硬塞 top_k 条最不相关的进提示词，
        反而诱导模型瞎编。阈值用回放数据标定（本项目实测相关命中
        在 0.6 以上，不相关的也在 0.5 附近，所以取 0.55）。

        dedup=True 时过滤近重复：同一句话在多个场次被反复刷、
        或者转录把同一句识别成两种写法时，它们会互相挤占 top_k，
        把真正有用的内容挤出去，模型也会以为"这件事发生了很多次"。
        """
        mat, idx_kinds, idx_refs = self._load()
        if len(mat) == 0:
            return []
        q = self.emb.encode([query], is_query=True)[0]
        sims = mat @ q
        out = []
        picked = []            # 已选中的行号，用于算两两相似度
        seen_text = set()      # 归一化后的文本，用于抓完全重复
        conn = self.store.conn
        for i in np.argsort(-sims):
            if len(out) >= top_k:
                break
            kind = idx_kinds[i]
            if kinds and kind not in kinds:
                continue
            if sims[i] < min_score:
                break          # 已按相似度降序，后面的只会更低
            row = conn.execute(
                "SELECT text, uname, ts, session_id, uid, is_bot "
                "FROM events WHERE id=? AND is_noise=0", (idx_refs[i],)).fetchone()
            if not row or row[5]:
                continue
            if exclude_session and row[3] == exclude_session:
                continue
            if dedup:
                key = _norm_key(row[0])
                if key in seen_text:
                    continue
                if any(float(mat[i] @ mat[j]) > 0.95 for j in picked):
                    continue
                seen_text.add(key)
                picked.append(i)
            out.append({"kind": kind, "text": row[0], "uname": row[1],
                        "ts": row[2], "session": row[3], "uid": row[4],
                        "score": float(sims[i])})
        return out


def _norm_key(text):
    """归一化文本，用来抓"只差标点/空格"的重复条目。"""
    import re
    return re.sub(r"[\s\W_]+", "", text or "")


def open_index(store, log=None, enabled=True):
    """尽力打开向量索引；模型缺失、显存不够等情况下返回 None。

    检索是"锦上添花"：拿不到就少一块上下文，绝不能让整套监听起不来。
    所以这里吞掉所有异常，只打一条日志。
    """
    if not enabled:
        return None
    import os
    os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
    try:
        index = VectorIndex(store, Embedder())
        n = store.conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0]
        if log:
            log("语义检索已启用（索引 %d 条，设备 %s）"
                % (n, index.emb.device))
        return index
    except Exception as exc:
        if log:
            log("语义检索不可用（%s: %s），本次运行不使用历史检索"
                % (type(exc).__name__, exc))
        return None
