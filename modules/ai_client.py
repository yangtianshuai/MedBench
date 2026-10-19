"""
AI 客户端（OpenAI 兼容接口）

只依赖 requests，不引入 openai SDK —— 打包体积不变，且能对接任何兼容服务：
DeepSeek / 通义 / 智谱 / 月之暗面 / Azure OpenAI / 本地 vLLM、Ollama、LM Studio 等。

面向本地模型的几点考虑：
- **代理**：本机与内网地址一律绕过系统代理。公司网络里配了 HTTP_PROXY 时，
  requests 会把 http://127.0.0.1:11434 也交给代理，表现为「地址明明对却连不上」。
- **流式**：可选 SSE 流式接收，边生成边回调。流式不只是「看得见进度」——
  requests 的 read timeout 在流式下是**两次数据之间的间隔**，因此本地大模型的
  长时间生成不会因为总耗时超限而被掐断。
- **模型名**：本地服务的模型名由用户环境决定（vLLM 的 --served-model-name、
  Ollama 的 qwen2.5:14b），因此提供 list_models() 从 /v1/models 直接列出来。

配置默认只存在当前会话；用户勾选「记住到本机」后才写入用户目录的明文 JSON，
界面提供一键清除。
"""
import json
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse

import requests

CONFIG_PATH = Path.home() / '医析台AI配置.json'
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 3600
ENDPOINT_SUFFIX = '/chat/completions'
MODELS_SUFFIX = '/models'


class AIError(RuntimeError):
    """带用户可读提示的调用错误。"""


@dataclass
class AIConfig:
    base_url: str = ''
    api_key: str = ''
    model: str = ''
    redact: bool = True
    timeout: int = DEFAULT_TIMEOUT

    def is_ready(self) -> bool:
        """地址与模型名都填了才认为可调用（本地 Ollama / vLLM 允许 Key 为空）。"""
        return bool(self.base_url.strip() and self.model.strip())

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Optional[Dict]) -> 'AIConfig':
        data = data or {}
        return cls(
            base_url=str(data.get('base_url', '') or ''),
            api_key=str(data.get('api_key', '') or ''),
            model=str(data.get('model', '') or ''),
            redact=bool(data.get('redact', True)),
            timeout=int(data.get('timeout', DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT),
        )


# ==================== 服务端统一配置 ====================

SERVER_CONFIG_NAME = 'ai_server_config.json'
ENV_PREFIX = 'MEDBENCH_'
ENV_KEYS = {'base_url': 'BASE_URL', 'api_key': 'API_KEY', 'model': 'MODEL',
            'timeout': 'TIMEOUT', 'redact': 'REDACT'}


def server_config_paths() -> List[Path]:
    """
    服务端统一配置的查找位置（按顺序找到即用）。

    覆盖两种部署习惯：把配置和程序放在一起（整包复制到服务器后改一份即可），
    或放在用户目录（同一台机器上多个账户共用一份）。环境变量优先级最高，
    适合容器 / systemd 这类不方便改文件的环境。
    """
    app_root = Path(__file__).resolve().parent.parent      # 仓库根目录
    return [
        app_root / SERVER_CONFIG_NAME,
        app_root.parent / SERVER_CONFIG_NAME,
        Path.home() / SERVER_CONFIG_NAME,
    ]


def env_config() -> Dict:
    """从环境变量读服务端配置（MEDBENCH_BASE_URL / MEDBENCH_API_KEY / …）。"""
    import os

    result: Dict = {}
    for key, suffix in ENV_KEYS.items():
        raw = os.environ.get(f'{ENV_PREFIX}{suffix}')
        if raw is None or str(raw).strip() == '':
            continue
        if key == 'timeout':
            try:
                result[key] = int(float(raw))
            except (TypeError, ValueError):
                continue
        elif key == 'redact':
            result[key] = str(raw).strip().lower() not in ('0', 'false', 'no', 'off')
        else:
            result[key] = str(raw).strip()
    return result


_SERVER_CONFIG_CACHE: Optional[Dict] = None


def clear_server_config_cache() -> None:
    """丢弃缓存（改了配置文件后重启程序即可，测试中也用它）。"""
    global _SERVER_CONFIG_CACHE
    _SERVER_CONFIG_CACHE = None


def load_server_config() -> Dict:
    """
    读取服务端统一配置。优先级：环境变量 > 共享配置文件。

    返回空字典表示没有做服务端配置，此时界面保持原来的「每人自己填」行为 ——
    单机使用完全不受影响。

    结果缓存：服务端配置存在时，每个会话每次渲染都会走到这里（因为会话里没有
    用户自己填的配置），不缓存的话会反复读文件。
    """
    global _SERVER_CONFIG_CACHE
    if _SERVER_CONFIG_CACHE is not None:
        return _SERVER_CONFIG_CACHE

    from_env = env_config()
    config: Dict = {}
    for path in server_config_paths():
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        if isinstance(data, dict):
            config = {key: value for key, value in data.items()
                      if value not in (None, '')}
            config['_source'] = str(path)
            break
    if from_env:
        config.update(from_env)
        config['_source'] = config.get('_source') or '环境变量'
    _SERVER_CONFIG_CACHE = config
    return config


def summarize_server_config(config: Dict) -> str:
    """给界面用的一句话摘要（**不回显 API Key**）。"""
    if not config:
        return ''
    parts = []
    if config.get('base_url'):
        parts.append(f"服务地址 {config['base_url']}")
    if config.get('model'):
        parts.append(f"模型 {config['model']}")
    parts.append('API Key 已配置' if config.get('api_key') else 'API Key 未设置')
    if config.get('_source'):
        parts.append(f"来源 {config['_source']}")
    return '；'.join(parts)


# ==================== 本地 / 内网地址与代理 ====================

def is_local_endpoint(base_url: str) -> bool:
    """
    是否为本机或内网地址。

    这类地址必须绕过系统代理：走代理不仅多一跳，多数代理也无法回连本机端口，
    结果就是「填了 127.0.0.1 却连不上」，而用户会误以为模型服务没起来。
    """
    raw = str(base_url or '').strip()
    if not raw:
        return False
    target = raw if '//' in raw else '//' + raw
    try:
        host = (urlparse(target).hostname or '').lower()
    except Exception:
        return False
    if not host:
        return False
    if host in ('localhost', '::1', '0.0.0.0'):
        return True
    if host.startswith('127.'):
        return True
    parts = host.split('.')
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        first, second = int(parts[0]), int(parts[1])
        if first == 10:
            return True
        if first == 172 and 16 <= second <= 31:
            return True
        if first == 192 and second == 168:
            return True
        if first == 169 and second == 254:
            return True
    return False


def proxies_for(base_url: str):
    """本地 / 内网地址返回空代理字典（绕过系统代理），其余返回 None（用默认行为）。"""
    return {'http': None, 'https': None} if is_local_endpoint(base_url) else None


# ==================== 地址规范化 ====================

def normalize_base(base_url: str) -> str:
    """把用户填写的地址规范化为服务根（形如 …/v1）。"""
    url = (base_url or '').strip().rstrip('/')
    if not url:
        raise AIError('请先填写 AI 服务地址。')
    lower = url.lower()
    if lower.endswith(ENDPOINT_SUFFIX):
        url = url[: -len(ENDPOINT_SUFFIX)].rstrip('/')
        lower = url.lower()
    if '/openai/deployments/' in lower or 'api-version=' in lower:
        return url
    if url.lower().endswith('/v1'):
        return url
    return url + '/v1'


def normalize_endpoint(base_url: str) -> str:
    """
    把用户填写的地址规范化为 chat/completions 端点。

    支持三种写法：
      https://api.deepseek.com                    -> …/v1/chat/completions
      http://127.0.0.1:8000/v1                    -> …/v1/chat/completions
      https://x.openai.azure.com/openai/deployments/d/chat/completions?api-version=…（原样使用）
    """
    url = (base_url or '').strip().rstrip('/')
    if not url:
        raise AIError('请先填写 AI 服务地址。')
    lower = url.lower()
    if lower.endswith(ENDPOINT_SUFFIX):
        return url
    if '/openai/deployments/' in lower or 'api-version=' in lower:
        return url
    return normalize_base(url) + ENDPOINT_SUFFIX


# ==================== 配置持久化 ====================

def load_saved_config() -> AIConfig:
    """读取本机保存的配置；不存在或损坏时返回空配置。"""
    if not CONFIG_PATH.is_file():
        return AIConfig()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except Exception:
        return AIConfig()
    return AIConfig.from_dict(data)


def save_config(config: AIConfig) -> Path:
    """把配置写入本机明文文件（界面需明确提示风险）。"""
    CONFIG_PATH.write_text(
        json.dumps(config.to_dict(), ensure_ascii=False, indent=2), encoding='utf-8')
    return CONFIG_PATH


def clear_saved_config() -> bool:
    """清除本机保存的配置，返回是否确有文件被删除。"""
    if CONFIG_PATH.is_file():
        CONFIG_PATH.unlink()
        return True
    return False


def _extract_error_text(response) -> str:
    try:
        body = response.json()
        error = body.get('error', body)
        if isinstance(error, dict):
            return str(error.get('message') or error)[:300]
        return str(error)[:300]
    except Exception:
        return (response.text or '')[:300]


def list_models(base_url: str, api_key: str = '', timeout: int = 15) -> List[str]:
    """
    列出服务提供的模型名（OpenAI 兼容 GET /v1/models）。

    本地服务的模型名由用户环境决定，猜错只会得到 400，因此这里直接向服务索取。
    vLLM、LM Studio、Ollama（OpenAI 兼容层）都提供该接口。
    """
    url = normalize_base(base_url) + MODELS_SUFFIX
    headers = {}
    key = (api_key or '').strip()
    if key:
        headers['Authorization'] = f'Bearer {key}'
    try:
        response = requests.get(url, headers=headers, timeout=timeout,
                                proxies=proxies_for(base_url))
    except requests.exceptions.Timeout:
        raise AIError(f'获取模型列表超时（{timeout} 秒）：请确认服务已启动。')
    except requests.exceptions.ConnectionError:
        raise AIError(f'无法连接 {url}\n'
                      '本地服务请确认已启动（如 vLLM / LM Studio 已运行、Ollama 已在后台），'
                      '并检查地址与端口。')
    except requests.exceptions.RequestException as error:
        raise AIError(f'获取模型列表失败：{error}')

    if response.status_code == 404:
        raise AIError(f'{url} 不存在（404）：该服务可能不提供 /v1/models 接口，'
                      '请手动填写模型名。')
    if response.status_code == 401:
        raise AIError('鉴权失败（401）：API Key 无效或已过期。')
    if response.status_code >= 400:
        raise AIError(f'服务返回 {response.status_code}：{_extract_error_text(response)}')

    try:
        body = response.json()
    except Exception:
        raise AIError('返回内容无法解析：该地址可能不是 OpenAI 兼容接口。')

    items = body.get('data') if isinstance(body, dict) else None
    if items is None and isinstance(body, dict):
        items = body.get('models')
    names: List[str] = []
    for item in items or []:
        if isinstance(item, dict):
            name = item.get('id') or item.get('name') or item.get('model')
        else:
            name = item
        if name:
            names.append(str(name))
    if not names:
        raise AIError('服务未返回任何模型：请确认服务已加载模型后再试。')
    return names


class AIClient:
    """最小可用的 OpenAI 兼容客户端。"""

    def __init__(self, config: AIConfig):
        self.config = config

    def chat(self, messages: List[Dict], temperature: float = 0.2,
             max_tokens: int = 4096,
             on_delta: Optional[Callable[[str], None]] = None) -> Dict:
        """
        调用一次对话补全，返回内容与耗时。失败时抛出带提示的 AIError。

        on_delta 非 None 时改用流式（SSE）接收，并逐段把增量文本回调出去。
        """
        if not self.config.is_ready():
            raise AIError('AI 尚未配置完成：请填写服务地址与模型名。')

        streaming = on_delta is not None
        endpoint = normalize_endpoint(self.config.base_url)
        headers = {'Content-Type': 'application/json'}
        api_key = self.config.api_key.strip()
        if api_key:
            headers['Authorization'] = f'Bearer {api_key}'
        payload = {
            'model': self.config.model.strip(),
            'messages': messages,
            'temperature': temperature,
            'stream': bool(streaming),
        }
        if max_tokens:
            payload['max_tokens'] = max_tokens

        timeout = int(self.config.timeout or DEFAULT_TIMEOUT)
        # 流式下 timeout 是「两次数据之间的间隔」而非总时长，长输出不会被掐断；
        # 非流式仍是整体超时，因此沿用配置值。
        request_timeout = (min(30, timeout), timeout) if streaming else timeout
        started = time.time()
        try:
            response = requests.post(
                endpoint, headers=headers, json=payload, timeout=request_timeout,
                proxies=proxies_for(self.config.base_url), stream=streaming)
        except requests.exceptions.Timeout:
            if streaming:
                raise AIError(f'流式接收超时（{timeout} 秒内没有新数据）：'
                              '可稍后重试，或在配置里调大超时时间。')
            raise AIError(f'请求超时（{timeout} 秒）：可稍后重试，或在配置里调大超时时间。')
        except requests.exceptions.SSLError as error:
            raise AIError(f'HTTPS 证书校验失败：{error}\n若为自建服务，请确认证书有效。')
        except requests.exceptions.ConnectionError:
            hint = ('本地服务请确认已启动（vLLM / LM Studio 已运行、Ollama 在后台），'
                    '并检查地址与端口。' if is_local_endpoint(self.config.base_url)
                    else '请检查服务地址、网络连通性与代理设置。')
            raise AIError(f'无法连接 {endpoint}\n{hint}')
        except requests.exceptions.RequestException as error:
            raise AIError(f'请求失败：{error}')

        status = response.status_code
        if status == 401:
            raise AIError('鉴权失败（401）：API Key 无效或已过期。')
        if status == 403:
            raise AIError('访问被拒（403）：请确认该 Key 有权调用此模型。')
        if status == 402:
            raise AIError('账户余额不足（402）：Key 本身是有效的，但账户没有可用额度，'
                          '请先到 AI 服务控制台充值后再试。')
        if status == 400:
            raise AIError(f'请求被拒（400）：{_extract_error_text(response)}\n'
                          '常见原因：模型名填写错误（如多打了空格、大小写不符）、'
                          '或该模型不支持当前请求参数。本地服务可用「📋 列出可用模型」核对。')
        if status == 404:
            raise AIError(f'接口不存在（404）：{endpoint}\n'
                          '多数服务需要 /v1 路径，本程序已自动补全；若仍失败请填写完整地址。')
        if status == 429:
            raise AIError('触发限流（429）：请稍后重试，或降低调用频率。')
        if status >= 400:
            raise AIError(f'服务返回 {status}：{_extract_error_text(response)}')

        if streaming:
            content = self._consume_stream(response, on_delta)
            elapsed = time.time() - started
            if not content.strip():
                raise AIError('流式响应中没有收到任何内容：该服务可能不支持流式输出，'
                              '请改用非流式接口或换用其他模型。')
            return {
                'content': content.strip(),
                'model': self.config.model,
                'elapsed': elapsed,
                'usage': {},
                'endpoint': endpoint,
                'streamed': True,
            }

        elapsed = time.time() - started
        try:
            data = response.json()
            content = data['choices'][0]['message']['content']
        except Exception:
            raise AIError('返回内容无法解析：该地址可能不是 OpenAI 兼容接口。')

        return {
            'content': str(content).strip(),
            'model': data.get('model') or self.config.model,
            'elapsed': elapsed,
            'usage': data.get('usage') or {},
            'endpoint': endpoint,
            'streamed': False,
        }

    @staticmethod
    def _consume_stream(response, on_delta: Callable[[str], None]) -> str:
        """解析 SSE 数据流，逐段回调增量文本，返回完整内容。"""
        chunks: List[str] = []
        try:
            for raw_line in response.iter_lines(decode_unicode=False):
                if not raw_line:
                    continue
                line = raw_line.decode('utf-8', 'replace').strip()
                if not line.startswith('data:'):
                    continue
                data = line[5:].strip()
                if data == '[DONE]':
                    break
                try:
                    packet = json.loads(data)
                except Exception:
                    continue
                choices = packet.get('choices') or [{}]
                delta = (choices[0].get('delta') or {}).get('content') or ''
                if delta:
                    chunks.append(delta)
                    on_delta(delta)
        except requests.exceptions.Timeout:
            raise AIError('流式接收中途超时：已生成的内容不完整，请重试或调大超时时间。')
        except requests.exceptions.RequestException as error:
            raise AIError(f'流式接收中断：{error}')
        return ''.join(chunks)

    def test_connection(self) -> Dict:
        """发一条极短请求验证连通性与鉴权。"""
        return self.chat(
            [{'role': 'user', 'content': '请只回复两个字：正常'}], max_tokens=16)

    @staticmethod
    def _extract_error(response) -> str:
        return _extract_error_text(response)
