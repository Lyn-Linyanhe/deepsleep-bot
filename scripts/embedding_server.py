# 本地 OpenAI 兼容 embedding 服务（fastembed + BAAI/bge-m3，1024 维）。
# 供 MaiBot 的 A_Memorix 记忆系统做语义向量化，替代不可用的中转站 embedding。
# 接口：POST /v1/embeddings（input: str | list[str]，可选 model/dimensions 参数，dimensions 忽略——bge-m3 固定 1024 维）。
from __future__ import annotations

import time
from typing import Any, List, Union

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from fastembed import TextEmbedding

MODEL_NAME = "Qwen/Qwen3-Embedding-0.6B"
DIMENSION = 1024
MAX_BATCH = 64

app = FastAPI(title="deepsleep local embedding")

_model: TextEmbedding | None = None


def get_model() -> TextEmbedding:
    global _model
    if _model is None:
        # 模型文件缓存在本目录 models/ 下，首次启动会下载约 1.2 GB
        _model = TextEmbedding(model_name=MODEL_NAME, cache_dir="models")
    return _model


class EmbeddingRequest(BaseModel):
    input: Union[str, List[str]]
    model: str | None = None
    dimensions: int | None = None
    encoding_format: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", "model": MODEL_NAME, "dimension": DIMENSION}


@app.get("/v1/models")
def list_models() -> dict[str, Any]:
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "local"}]}


@app.post("/v1/embeddings")
def embeddings(req: EmbeddingRequest) -> dict[str, Any]:
    texts = [req.input] if isinstance(req.input, str) else list(req.input)
    texts = [t if isinstance(t, str) else str(t) for t in texts]
    if not texts or any(not t.strip() for t in texts):
        raise HTTPException(status_code=400, detail="input 不能为空")
    try:
        model = get_model()
        vectors: List[List[float]] = []
        for i in range(0, len(texts), MAX_BATCH):
            batch = list(model.embed(texts[i : i + MAX_BATCH]))
            vectors.extend(v.tolist() for v in batch)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 对外统一 500，本地服务无需细分
        raise HTTPException(status_code=500, detail=f"embedding failed: {exc}") from exc
    if len(vectors) != len(texts):
        raise HTTPException(status_code=500, detail=f"返回数量不符: {len(vectors)} != {len(texts)}")
    return {
        "object": "list",
        "data": [
            {"object": "embedding", "index": i, "embedding": v}
            for i, v in enumerate(vectors)
        ],
        "model": MODEL_NAME,
        "usage": {"prompt_tokens": sum(len(t) for t in texts), "total_tokens": sum(len(t) for t in texts)},
    }


@app.on_event("startup")
def warmup() -> None:
    get_model()
    vec = next(get_model().embed(["预热"]))
    assert len(vec) == DIMENSION, f"维度异常: {len(vec)} != {DIMENSION}"
    print(f"[embedding] {MODEL_NAME} 就绪，维度 {DIMENSION}，耗时校验通过 {time.strftime('%H:%M:%S')}")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8100, log_level="info")
