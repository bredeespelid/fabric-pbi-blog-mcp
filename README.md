# fabric-pbi-blog-mcp

MCP server (streamable HTTP) for the official Microsoft blogs on community.fabric.microsoft.com:

- [Power BI Updates Blog](https://community.fabric.microsoft.com/category/fabricupdatesblogs/blog/fbc_pbiupdatesblog) (`powerbi`)
- [Fabric Updates Blog](https://community.fabric.microsoft.com/category/fabricupdatesblogs/blog/fbc_fabricupdatesblogs) (`fabric`)

Data comes from the boards' public RSS feeds (latest 20 posts each, full text), cached for 15 minutes.
Posts seen since the server started are kept in memory, so history grows while the process runs.

## Tools

| Tool | Description |
|---|---|
| `list_blogs` | The blogs covered |
| `latest_posts(blog, limit, since)` | Newest posts, optionally since an ISO date |
| `search_posts(query, blog, limit)` | Keyword search in titles and full text |
| `get_post(id_or_url)` | Full text of one post |

`blog` is `powerbi`, `fabric` or `all`.

## Run locally

```bash
pip install -r requirements.txt
python server.py   # MCP endpoint: http://localhost:8000/mcp
```

## Deploy on Render

`render.yaml` defines a free Python web service. Connect the MCP client to `https://<service>.onrender.com/mcp`.
