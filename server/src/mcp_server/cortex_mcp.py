"""Memory Cortex as MCP Server — 让任意 LLM 通过 Model Context Protocol 调用 cortex.

不使用 FastMCP (避免额外 deps), 直接用 stdlib + JSON-RPC 2.0 协议 (MCP 用 HTTP+JSON-RPC).

启动:
    python -m src.mcp_server.cortex_mcp           # stdio transport (default)
    python -m src.mcp_server.cortex_mcp --http 9000  # HTTP transport

API 暴露 (符合 MCP tool schema):
  remember(content, type?, tags?, importance?) → chunk_id
  recall(query, top_k?) → chunks[]
  forget(chunk_id) → status
  reason(question, top_k?) → {chunks, context}
  introspect() → {health, version, totals}

Note: 这个文件可在 任意 MCP-compatible client 中接入 (Claude Desktop / Cursor / trae / Claude Code).
"""

import json
import sys
import argparse
import os
from typing import Any, Dict, List

# Add parent dir for sys.path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from src.sdk.cortex_client import MemoryCortexClient


# ========== MCP Server Core ==========

class MCPServer:
    """Minimal MCP server using stdio transport (Claude Desktop compatible)."""

    PROTOCOL_VERSION = "2024-11-05"
    SERVER_NAME = "memory-cortex"
    SERVER_VERSION = "4.3.0"

    def __init__(self, base_url: str = "http://localhost:1880"):
        self.client = MemoryCortexClient(base_url)
        self.tools = {
            'remember': {
                'description': 'Store a memory chunk (auto CoALA decision). Returns chunk_id.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'content': {'type': 'string', 'description': 'Memory content'},
                        'type': {'type': 'string', 'default': 'knowledge'},
                        'tags': {'type': 'array', 'items': {'type': 'string'}, 'default': []},
                        'importance': {'type': 'integer', 'default': 5, 'minimum': 1, 'maximum': 10},
                    },
                    'required': ['content'],
                },
                'handler': self._tool_remember,
            },
            'recall': {
                'description': 'Search and retrieve relevant memories for a query.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'query': {'type': 'string', 'description': 'Search query'},
                        'top_k': {'type': 'integer', 'default': 5, 'minimum': 1, 'maximum': 20},
                    },
                    'required': ['query'],
                },
                'handler': self._tool_recall,
            },
            'forget': {
                'description': 'Delete or archive a memory chunk.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'chunk_id': {'type': 'string', 'description': 'ID of memory to forget'},
                    },
                    'required': ['chunk_id'],
                },
                'handler': self._tool_forget,
            },
            'reason': {
                'description': 'RAG-style reasoning: recall relevant memories and build context.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'question': {'type': 'string', 'description': 'Question to reason about'},
                        'top_k': {'type': 'integer', 'default': 5},
                    },
                    'required': ['question'],
                },
                'handler': self._tool_reason,
            },
            'introspect': {
                'description': 'Get self-aware metadata: health, stats, ops counters.',
                'parameters': {
                    'type': 'object',
                    'properties': {},
                },
                'handler': self._tool_introspect,
            },
            'consolidate': {
                'description': 'G2: apply extracted facts to user core profile (consolidation).',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'user_id': {'type': 'string'},
                        'min_confidence': {'type': 'number', 'default': 0.5},
                    },
                    'required': ['user_id'],
                },
                'handler': self._tool_consolidate,
            },
                        'remember_episode': {
                'description': 'CFGM: remember an episode (chunk group with session/title/summary).',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'session_id': {'type': 'string'},
                        'title': {'type': 'string'},
                        'summary': {'type': 'string'},
                        'parent_chunk_id': {'type': 'string'},
                    },
                    'required': ['session_id', 'title', 'summary'],
                },
                'handler': self._tool_remember_episode,
            },
            'learn_procedure': {
                'description': 'PlugMem: learn a trigger→action procedure (procedural memory).',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'name': {'type': 'string'},
                        'trigger': {'type': 'string'},
                        'action': {'type': 'string'},
                        'description': {'type': 'string'},
                    },
                    'required': ['name', 'trigger', 'action'],
                },
                'handler': self._tool_learn_procedure,
            },
            'find_procedure': {
                'description': 'PlugMem: find matching procedure by trigger query.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'trigger_query': {'type': 'string'},
                        'limit': {'type': 'integer', 'default': 5},
                    },
                    'required': ['trigger_query'],
                },
                'handler': self._tool_find_procedure,
            },
            'reconsolidate': {
                'description': 'Reconsolidate: strengthen/weaken memories based on access + age.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'memory_id': {'type': 'string', 'default': None},
                    },
                },
                'handler': self._tool_reconsolidate,
            },
            'merge_duplicates': {
                'description': 'Merge near-duplicate chunks (similarity >= threshold).',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'threshold': {'type': 'number', 'default': 0.85},
                    },
                },
                'handler': self._tool_merge_duplicates,
            },
            'forgetting_stats': {
                'description': 'Forgetting curve stats (how many memories expire soon, already expired).',
                'parameters': {
                    'type': 'object',
                    'properties': {},
                },
                'handler': self._tool_forgetting_stats,
            },
            'ruminate': {
                'description': 'Rumination: cluster similar memories → abstract heuristics.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'min_cluster_size': {'type': 'integer', 'default': 3},
                    },
                },
                'handler': self._tool_ruminate,
            },
            'deep_context': {
                'description': 'Deep context for one memory: chunk + provenance + history + relations + hierarchy + links.',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'memory_id': {'type': 'string'},
                    },
                    'required': ['memory_id'],
                },
                'handler': self._tool_deep_context,
            },
            'agent_health': {
                'description': 'All-in-one health check: cortex server + stats + cache + storage + forgetting.',
                'parameters': {
                    'type': 'object',
                    'properties': {},
                },
                'handler': self._tool_agent_health,
            },
            'submit_feedback': {
                'description': 'Submit eval feedback (RLHF for memory).',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'memory_id': {'type': 'string'},
                        'feedback_type': {'type': 'string'},
                        'score': {'type': 'number'},
                        'comment': {'type': 'string', 'default': ''},
                    },
                    'required': ['memory_id', 'feedback_type', 'score'],
                },
                'handler': self._tool_submit_feedback,
            },
            'storage_health': {
                'description': 'Get storage health (chroma + sqlite + vector count).',
                'parameters': {
                    'type': 'object',
                    'properties': {},
                },
                'handler': self._tool_storage_health,
            },
            'build_context': {
                'description': 'G1: build LLM context from working memory + conversation (per session).',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'session_id': {'type': 'string'},
                    },
                    'required': ['session_id'],
                },
                'handler': self._tool_build_context,
            },
        }

    def _tool_remember(self, args: Dict) -> Dict:
        chunk = self.client.remember(
            content=args['content'],
            type=args.get('type', 'knowledge'),
            tags=args.get('tags', []),
            importance=args.get('importance', 5),
        )
        return {'chunk_id': chunk.get('id'), 'type': chunk.get('type'),
                'importance': chunk.get('importance'),
                'tier': chunk.get('tier')}

    def _tool_recall(self, args: Dict) -> Dict:
        results = self.client.recall(args['query'], top_k=args.get('top_k', 5))
        chunks = results if isinstance(results, list) else results.get('results', [])
        return {
            'count': len(chunks),
            'chunks': [
                {
                    'id': c.get('id', ''),
                    'content': c.get('content', '')[:200],
                    'type': c.get('type', ''),
                    'similarity': c.get('similarity', 0.0),
                    'tags': c.get('tags', []),
                } for c in chunks
            ],
        }

    def _tool_forget(self, args: Dict) -> Dict:
        result = self.client.forget(chunk_id=args['chunk_id'])
        return result

    def _tool_reason(self, args: Dict) -> Dict:
        r = self.client.reason(args['question'], top_k=args.get('top_k', 5))
        return {
            'question': args['question'],
            'context_chunks': r['context_chunks'][:1500],
            'chunks_used': len(r['chunks']),
            'hierarchies_attached': len(r['hierarchies']),
            'links_attached': len(r['links']),
        }

    def _tool_introspect(self, args: Dict) -> Dict:
        return self.client.introspect()

    def _tool_consolidate(self, args: Dict) -> Dict:
        return self.client.consolidate_facts(
            user_id=args['user_id'],
            min_confidence=args.get('min_confidence', 0.5),
        )

    def _tool_remember_episode(self, args: Dict) -> Dict:
        return self.client.remember_episode(
            session_id=args['session_id'],
            title=args['title'],
            summary=args['summary'],
            parent_chunk_id=args.get('parent_chunk_id'),
        )

    def _tool_learn_procedure(self, args: Dict) -> Dict:
        return self.client.learn_procedure(
            name=args['name'],
            trigger=args['trigger'],
            action=args['action'],
            description=args.get('description', ''),
        )

    def _tool_find_procedure(self, args: Dict) -> Dict:
        return {'procedures': self.client.find_procedure(
            trigger_query=args['trigger_query'],
            limit=args.get('limit', 5),
        )}

    def _tool_reconsolidate(self, args: Dict) -> Dict:
        n = self.client.reconsolidate(memory_id=args.get('memory_id'))
        return {'affected': n}

    def _tool_merge_duplicates(self, args: Dict) -> Dict:
        n = self.client.merge_duplicates(threshold=args.get('threshold', 0.85))
        return {'merged_count': n}

    def _tool_forgetting_stats(self, args: Dict) -> Dict:
        return self.client.forgetting_stats()

    def _tool_ruminate(self, args: Dict) -> Dict:
        return self.client.ruminate(
            min_cluster_size=args.get('min_cluster_size', 3),
        )

    def _tool_deep_context(self, args: Dict) -> Dict:
        return self.client.deep_context(args['memory_id'])

    def _tool_agent_health(self, args: Dict) -> Dict:
        return self.client.agent_health()

    def _tool_submit_feedback(self, args: Dict) -> Dict:
        ok = self.client.submit_feedback(
            memory_id=args['memory_id'],
            feedback_type=args['feedback_type'],
            score=args['score'],
            comment=args.get('comment', ''),
        )
        return {'recorded': ok}

    def _tool_storage_health(self, args: Dict) -> Dict:
        return self.client.storage_health()

    def _tool_build_context(self, args: Dict) -> Dict:
        return self.client.build_context(args['session_id'])

    # ========== JSON-RPC 2.0 / MCP Protocol ==========

    def _make_response(self, req_id: Any, result: Any) -> Dict:
        return {'jsonrpc': '2.0', 'id': req_id, 'result': result}

    def _make_error(self, req_id: Any, code: int, message: str) -> Dict:
        return {'jsonrpc': '2.0', 'id': req_id,
                'error': {'code': code, 'message': message}}

    def handle_request(self, req: Dict) -> Dict:
        """Route incoming JSON-RPC request."""
        method = req.get('method')
        params = req.get('params', {})
        req_id = req.get('id')

        if method == 'initialize':
            return self._make_response(req_id, {
                'protocolVersion': self.PROTOCOL_VERSION,
                'serverInfo': {'name': self.SERVER_NAME, 'version': self.SERVER_VERSION},
                'capabilities': {'tools': {'listChanged': False}},
            })
        elif method == 'notifications/initialized':
            return None
        elif method == 'tools/list':
            return self._make_response(req_id, {
                'tools': [
                    {
                        'name': name,
                        'description': spec['description'],
                        'inputSchema': spec['parameters'],
                    }
                    for name, spec in self.tools.items()
                ]
            })
        elif method == 'tools/call':
            tool_name = params.get('name')
            args = params.get('arguments', {})
            if tool_name not in self.tools:
                return self._make_error(req_id, -32601, f'Unknown tool: {tool_name}')
            try:
                result = self.tools[tool_name]['handler'](args)
                return self._make_response(req_id, {
                    'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}],
                    'isError': False,
                })
            except Exception as e:
                return self._make_response(req_id, {
                    'content': [{'type': 'text', 'text': f'Error: {str(e)}'}],
                    'isError': True,
                })
        else:
            return self._make_error(req_id, -32601, f'Unknown method: {method}')

    def serve_stdio(self):
        """Stdio JSON-RPC server (Claude Desktop compatible)."""
        while True:
            line = sys.stdin.readline()
            if not line:
                break
            try:
                req = json.loads(line)
                resp = self.handle_request(req)
                if resp is not None:
                    sys.stdout.write(json.dumps(resp, ensure_ascii=False) + '\n')
                    sys.stdout.flush()
            except json.JSONDecodeError:
                pass
            except Exception as e:
                sys.stderr.write(f'MCP error: {e}\n')

    def serve_http(self, port: int = 9000):
        """Simple HTTP JSON-RPC server (stdio only is standard MCP, but useful for testing)."""
        from http.server import HTTPServer, BaseHTTPRequestHandler

        server = self

        class MCPHandler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                body = self.rfile.read(length).decode('utf-8')
                try:
                    req = json.loads(body)
                    resp = server.handle_request(req)
                    payload = json.dumps(resp, ensure_ascii=False).encode('utf-8') if resp else b'{}'
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except Exception as e:
                    payload = json.dumps({'error': str(e)}).encode('utf-8')
                    self.send_response(500)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(payload)

        httpd = HTTPServer(('127.0.0.1', port), MCPHandler)
        sys.stderr.write(f'MCP HTTP server listening on port {port}\n')
        httpd.serve_forever()


# ========== CLI Entry ==========

def main():
    parser = argparse.ArgumentParser(description='Memory Cortex MCP Server')
    parser.add_argument('--base-url', default='http://localhost:1880',
                        help='Memory Cortex base URL (default: http://localhost:1880)')
    parser.add_argument('--http', type=int, metavar='PORT',
                        help='HTTP mode (default: stdio)')
    args = parser.parse_args()

    server = MCPServer(args.base_url)
    if args.http:
        server.serve_http(args.http)
    else:
        server.serve_stdio()


if __name__ == '__main__':
    main()
