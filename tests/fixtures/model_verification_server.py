"""Local synthetic OpenAI endpoint for manual/full-stack model acceptance.

Run: python tests/fixtures/model_verification_server.py
Only aggregate protocol metadata is retained, never prompts or credentials.
"""
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

calls, lock = [], Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        if self.path.endswith('/models'):
            body = json.dumps({'object': 'list', 'data': [
                {'id': 'model-a', 'name': 'Verification A'},
                {'id': 'model-b', 'name': 'Verification B'},
                {'id': 'qwen/team-tools', 'name': 'Qwen · Graph tools'},
                {'id': 'gemini/team-vision', 'name': 'Gemini · Vision'},
            ]}).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)
            return
        with lock:
            body = json.dumps(calls).encode()
        self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(body)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))))
        messages = body.get('messages', [])
        with lock:
            calls.append({'model': body.get('model'), 'path': self.path, 'roles': [item['role'] for item in messages],
                          'tools': len(body.get('tools', [])), 'stream': body.get('stream', False)})
        slow = any('verification-slow' in str(item.get('content')) for item in messages if item['role'] == 'user')
        if slow:
            time.sleep(4)
        result = {'id': 'chatcmpl-verification', 'created': int(time.time()), 'model': body['model']}
        text = 'Synthetic verification reply from ' + body['model']
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream' if body.get('stream') else 'application/json')
        self.end_headers()
        try:
            if body.get('stream'):
                for content in [text[:15], text[15:]]:
                    row = {**result, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {'content': content}, 'finish_reason': None}]}
                    self.wfile.write(('data: ' + json.dumps(row) + '\n\n').encode()); self.wfile.flush()
                self.wfile.write(('data: ' + json.dumps({**result, 'object': 'chat.completion.chunk', 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}) + '\n\ndata: [DONE]\n\n').encode())
            else:
                self.wfile.write(json.dumps({**result, 'object': 'chat.completion', 'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}]}).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


if __name__ == '__main__':
    print('Synthetic model endpoint: port 21119', flush=True)
    ThreadingHTTPServer(('0.0.0.0', 21119), Handler).serve_forever()
