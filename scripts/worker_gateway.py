"""Tunnel target exposing only authenticated Colab transport routes."""
import re
import httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse

app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)

@app.api_route('/api/colab-worker/{path:path}',methods=['GET','POST','PUT'])
async def forward(path: str, request: Request):
    if not re.fullmatch(r'hello|claim|[a-f0-9]{32}/(files|heartbeat|output|finish)', path):
        raise HTTPException(404, 'Not found')
    if not request.headers.get('authorization','').startswith('Bearer '):
        raise HTTPException(401,'Worker token required')
    # The upstream origin is fixed; arbitrary user URLs are never proxied.
    client=httpx.AsyncClient(timeout=180)
    url='http://127.0.0.1:8000/api/colab-worker/'+path
    req=client.build_request(request.method,url,params=request.query_params,
        headers={k:v for k,v in request.headers.items() if k.lower() in {'authorization','content-type'}},
        content=request.stream())
    try:
        response=await client.send(req,stream=True)
    except Exception:
        await client.aclose();raise
    async def body():
        try:
            async for chunk in response.aiter_bytes():yield chunk
        finally:
            await response.aclose();await client.aclose()
    return StreamingResponse(body(),status_code=response.status_code,
        headers={'content-type':response.headers.get('content-type','application/octet-stream'),
                 'cache-control':'no-store'})
