from fastapi import (
    FastAPI,
    Request,
    Response,
    HTTPException,
    Depends,
    status,
    UploadFile,
    File,
    BackgroundTasks,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.concurrency import run_in_threadpool

from pydantic import BaseModel, ConfigDict

import os
import re
import copy
import random
import requests
import json
import uuid
import aiohttp
import asyncio
import logging
import time
from datetime import datetime
from urllib.parse import urlparse
from typing import Optional, List, Union

from starlette.background import BackgroundTask

from apps.webui.models.models import Models
from apps.webui.models.users import Users
from constants import ERROR_MESSAGES
from utils.utils import (
    decode_token,
    get_current_user,
    get_verified_user,
    get_admin_user,
)
from utils.task import prompt_template

from config import (
    SRC_LOG_LEVELS,
    AIME_API_BASE_URLS,
    ENABLE_AIME_API,
    AIME_API_USERS,
    AIME_API_KEYS,
    AIOHTTP_CLIENT_TIMEOUT,
    ENABLE_MODEL_FILTER,
    MODEL_FILTER_LIST,
    UPLOAD_DIR,
    AppConfig,
)
from utils.misc import calculate_sha256, add_or_update_system_message

from aime_api_client_interface import ModelAPI

log = logging.getLogger(__name__)
log.setLevel(SRC_LOG_LEVELS["OLLAMA"])

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.config = AppConfig()

app.state.config.ENABLE_MODEL_FILTER = ENABLE_MODEL_FILTER
app.state.config.MODEL_FILTER_LIST = MODEL_FILTER_LIST

app.state.config.ENABLE_AIME_API = ENABLE_AIME_API
app.state.config.AIME_API_BASE_URLS = AIME_API_BASE_URLS
app.state.config.AIME_API_USERS = AIME_API_USERS
app.state.config.AIME_API_KEYS = AIME_API_KEYS
app.state.MODELS = {}

model_api = ModelAPI(
    app.state.config.AIME_API_BASE_URLS[0],
    'llama3_chat',
    session=aiohttp.ClientSession(
    trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    )
)



@app.middleware("http")
async def check_url(request: Request, call_next):
    if len(app.state.MODELS) == 0:
        await get_all_models()
    else:
        pass

    response = await call_next(request)
    return response


@app.head("/")
@app.get("/")
async def get_status():
    return {"status": True}


@app.get("/api/login")
@app.get("/api/login/{url_idx}")
async def aime_api_login(url_idx: Optional[int] = None):
    url_idx = 0 if url_idx is None else url_idx
    global model_api
    model_api.api_server = app.state.config.AIME_API_BASE_URLS[url_idx]
    try:
        key = await model_api.do_api_login_async(
            user=app.state.config.AIME_API_USERS[url_idx],
            key=app.state.config.AIME_API_KEYS[url_idx]
        )
        return key
    except ConnectionError as e:
        log.exception(e)
        error_detail = f"Open WebUI: Server Connection Error {e}"
        raise HTTPException(
            status_code=500,
            detail=error_detail,
    )


@app.get("/config")
async def get_config(user=Depends(get_admin_user)):
    return {"ENABLE_AIME_API": app.state.config.ENABLE_AIME_API}


class AIMEConfigForm(BaseModel):
    enable_aime_API: Optional[bool] = None


@app.post("/config/update")
async def update_config(form_data: AIMEConfigForm, user=Depends(get_admin_user)):
    app.state.config.ENABLE_AIME_API = form_data.enable_aime_API
    return {"ENABLE_AIME_API": app.state.config.ENABLE_AIME_API}


@app.get("/urls")
async def get_aime_api_urls(user=Depends(get_admin_user)):
    return {"AIME_API_BASE_URLS": app.state.config.AIME_API_BASE_URLS}


class UrlUpdateForm(BaseModel):
    urls: List[str]


class UsersAndKeysUpdateForm(BaseModel):
    users: List[str]
    keys: List[str]

class UsersUpdateForm(BaseModel):
    users: List[str]


@app.post("/urls/update")
async def update_aime_api_url(form_data: UrlUpdateForm, user=Depends(get_admin_user)):
    app.state.config.AIME_API_BASE_URLS = form_data.urls

    log.info(f"app.state.config.AIME_API_BASE_URLS: {app.state.config.AIME_API_BASE_URLS}")
    return {"AIME_API_BASE_URLS": app.state.config.AIME_API_BASE_URLS}


@app.get("/usersandkeys")
async def get_aime_users_and_keys(user=Depends(get_admin_user)):
    return {
        "AIME_API_USERS": app.state.config.AIME_API_USERS, 
        "AIME_API_KEYS": app.state.config.AIME_API_KEYS
    }


@app.post("/usersandkeys/update")
async def update_aime_user_and_key(form_data: UsersAndKeysUpdateForm, user=Depends(get_admin_user)):
    app.state.config.AIME_API_USERS = form_data.users
    app.state.config.AIME_API_KEYS = form_data.keys
    return {
        "AIME_API_USERS": app.state.config.AIME_API_USERS, 
        "AIME_API_KEYS": app.state.config.AIME_API_KEYS
    }



async def fetch_url(url):
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.get(url) as response:
                return await response.json()
    except Exception as e:
        # Handle connection error here
        log.error(f"Connection error: {e}")
        return None


async def cleanup_response(
    response: Optional[aiohttp.ClientResponse],
    session: Optional[aiohttp.ClientSession],
):
    if response:
        response.close()
    if session:
        await session.close()


async def post_streaming_url(url: str, payload: str, stream: bool = True, ):
    r = None
    try:
        session = aiohttp.ClientSession(
            trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
        )

        r = await session.post(url, data=payload)
        r.raise_for_status()
        if stream:
            
            return StreamingResponse(
                r.content,
                status_code=r.status,
                headers=dict(r.headers),
                background=BackgroundTask(
                    cleanup_response, response=r, session=session
                ),
            )
        else:
            res = await r.json()
            await cleanup_response(r, session)
            return res

    except Exception as e:
        error_detail = "Open WebUI: Server Connection Error"
        if r is not None:
            try:
                res = await r.json()
                if "error" in res:
                    error_detail = f"Ollama: {res['error']}"
            except:
                error_detail = f"Ollama: {e}"

        raise HTTPException(
            status_code=r.status if r else 500,
            detail=error_detail,
        )


def merge_models_lists(model_lists):
    merged_models = {}

    for idx, model_list in enumerate(model_lists):
        if model_list is not None:
            for model in model_list:
                digest = model["digest"]
                if digest not in merged_models:
                    model["urls"] = [idx]
                    merged_models[digest] = model
                else:
                    merged_models[digest]["urls"].append(idx)

    return list(merged_models.values())


async def get_all_models():
    log.info("get_all_models()")
    """
    if app.state.config.ENABLE_AIME_API:
        tasks = [
            fetch_url(f"{url}/api/tags") for url in app.state.config.AIME_API_BASE_URLS
        ]
        responses = await asyncio.gather(*tasks)

        models = {
            "models": merge_models_lists(
                map(
                    lambda response: response["models"] if response else None, responses
                )
            )
        }

    else:
        models = {"models": []}
    """
    if app.state.config.ENABLE_AIME_API:

        models = {
            'models': [
                {
                    'name': 'llama3-aime', 
                    'model': 'llama3_chat',
                    'details': {
                        'parent_model': '',
                        'format': 'gguf', 
                        'family': 'llama', 
                        'families': ['llama'], 
                        'parameter_size': '8.0B', 
                        'quantization_level': 'Q4_0'
                    }, 
                    'urls': [0]
                },
                            {
                'name': 'mixtral-aime', 
                'model': 'mixtral_chat',
                'details': {
                    'parent_model': '',
                    'format': 'gguf', 
                    'family': 'llama', 
                    'families': ['llama'], 
                    'parameter_size': '8.0B', 
                    'quantization_level': 'Q4_0'
                }, 
                'urls': [0]
                }
            ]
        }
    else:
        models = {"models": []}
    app.state.MODELS = {model["model"]: model for model in models["models"]}
    return models


@app.get("/api/tags")
@app.get("/api/tags/{url_idx}")
async def get_tags(
    url_idx: Optional[int] = None, user=Depends(get_verified_user)
):
    if url_idx == None:
        models = await get_all_models()
        if app.state.config.ENABLE_MODEL_FILTER:
            if user.role == "user":
                models["models"] = list(
                    filter(
                        lambda model: model["name"]
                        in app.state.config.MODEL_FILTER_LIST,
                        models["models"],
                    )
                )
                return models
        return models
    else:
        pass



class ModelNameForm(BaseModel):
    name: str


class GenerateEmbeddingsForm(BaseModel):
    model: str
    prompt: str
    options: Optional[dict] = None
    keep_alive: Optional[Union[int, str]] = None


@app.post("/api/embeddings")
@app.post("/api/embeddings/{url_idx}")
async def generate_embeddings(
    form_data: GenerateEmbeddingsForm,
    url_idx: Optional[int] = None,
    user=Depends(get_verified_user),
):
    if url_idx == None:
        model = form_data.model

        if ":" not in model:
            model = f"{model}:latest"

        if model in app.state.MODELS:
            url_idx = random.choice(app.state.MODELS[model]["urls"])
        else:
            raise HTTPException(
                status_code=400,
                detail=ERROR_MESSAGES.MODEL_NOT_FOUND(form_data.model),
            )

    url = app.state.config.AIME_API_BASE_URLS[url_idx]
    log.info(f"url: {url}")

    try:
        r = requests.request(
            method="POST",
            url=f"{url}/api/embeddings",
            data=form_data.model_dump_json(exclude_none=True).encode(),
        )
        r.raise_for_status()

        return r.json()
    except Exception as e:
        log.exception(e)
        error_detail = "Open WebUI: Server Connection Error"
        if r is not None:
            try:
                res = r.json()
                if "error" in res:
                    error_detail = f"Ollama: {res['error']}"
            except:
                error_detail = f"Ollama: {e}"

        raise HTTPException(
            status_code=r.status_code if r else 500,
            detail=error_detail,
        )


def generate_ollama_embeddings(
    form_data: GenerateEmbeddingsForm,
    url_idx: Optional[int] = None,
):

    log.info(f"generate_ollama_embeddings {form_data}")

    if url_idx == None:
        model = form_data.model

        if ":" not in model:
            model = f"{model}:latest"

        if model in app.state.MODELS:
            url_idx = random.choice(app.state.MODELS[model]["urls"])
        else:
            raise HTTPException(
                status_code=400,
                detail=ERROR_MESSAGES.MODEL_NOT_FOUND(form_data.model),
            )

    url = app.state.config.AIME_API_BASE_URLS[url_idx]
    log.info(f"url: {url}")

    try:
        r = requests.request(
            method="POST",
            url=f"{url}/api/embeddings",
            data=form_data.model_dump_json(exclude_none=True).encode(),
        )
        r.raise_for_status()

        data = r.json()

        log.info(f"generate_ollama_embeddings {data}")

        if "embedding" in data:
            return data["embedding"]
        else:
            raise "Something went wrong :/"
    except Exception as e:
        log.exception(e)
        error_detail = "Open WebUI: Server Connection Error"
        if r is not None:
            try:
                res = r.json()
                if "error" in res:
                    error_detail = f"Ollama: {res['error']}"
            except:
                error_detail = f"Ollama: {e}"

        raise error_detail


class ChatMessage(BaseModel):
    role: str
    content: str
    images: Optional[List[str]] = None


class GenerateChatCompletionForm(BaseModel):
    model: str
    messages: List[ChatMessage]
    format: Optional[str] = None
    options: Optional[dict] = None
    template: Optional[str] = None
    stream: Optional[bool] = None
    keep_alive: Optional[Union[int, str]] = None
    metadata: Optional[dict]
    max_tokens: Optional[int] = None


class AIMEResponseHandler():
    def __init__(self, model_name):
        self.model_name = model_name
        self.queue = asyncio.Queue()
        self.current_generated_text = ''
        self.finished = False

    async def progress_callback(self, progress_info, progress_data):
        if progress_data:
            chunk = progress_data.get('text', '').replace(self.current_generated_text, '')
            self.current_generated_text = progress_data.get('text', '')
            await self.queue.put(
                {
                    'model': self.model_name,
                    'created_at': time.time(),
                    'message': {
                        'role': 'assistant',
                        'content': chunk
                    },
                    'done': False
                }
            )

    async def result_callback(self, result):
        self.finished = True
        if result:
            await self.queue.put(
                    {
                    'model': self.model_name,
                    'created_at': time.time(),
                    'message': {
                        'role': 'assistant',
                        'content': result.get('text', '')
                    },
                    'done': True
                }
            )

    async def streaming_generator(self):
        done = False
        while not done:            
            chunk = await self.queue.get()
            done = chunk.get('done')
            yield json.dumps(chunk) + '\n'


async def generate_title(
    form_data: dict,
    url_idx: Optional[int] = None,
    user=Depends(get_verified_user),
):
    form_data.get('messages', [{}]).insert(
        0,
        {
            'role': 'system',
            'content': 'You create short titles! Your reply only contains the title!'
        }
    )
    return await generate_chat_completion(
        GenerateChatCompletionForm(**form_data),
        url_idx,
        user
    )


@app.post("/api/chat")
@app.post("/api/chat/{url_idx}")
async def generate_chat_completion(
    form_data: GenerateChatCompletionForm,
    url_idx: Optional[int] = None,
    user=Depends(get_verified_user),
):
    log.debug(
        "form_data.model_dump_json(exclude_none=True).encode(): {0} ".format(
            form_data.model_dump_json(exclude_none=True).encode()
        )
    )

    payload = {
        **form_data.model_dump(exclude_none=True),
    }

    model_id = form_data.model
    model_info = Models.get_model_by_id(model_id)
    if model_info:
        if model_info.base_model_id:
            payload["model"] = model_info.base_model_id

        model_info.params = model_info.params.model_dump()

        if model_info.params:
            if payload.get("options") is None:
                payload["options"] = {}
            if (
                model_info.params.get("mirostat", None)
                and payload["options"].get("mirostat") is None
            ):
                payload["options"]["mirostat"] = model_info.params.get("mirostat", None)

            if (
                model_info.params.get("mirostat_eta", None)
                and payload["options"].get("mirostat_eta") is None
            ):
                payload["options"]["mirostat_eta"] = model_info.params.get(
                    "mirostat_eta", None
                )
            if (
                model_info.params.get("mirostat_tau", None)
                and payload["options"].get("mirostat_tau") is None
            ):
                payload["options"]["mirostat_tau"] = model_info.params.get(
                    "mirostat_tau", None
                )

            if (
                model_info.params.get("num_ctx", None)
                and payload["options"].get("num_ctx") is None
            ):
                payload["options"]["num_ctx"] = model_info.params.get("num_ctx", None)

            if (
                model_info.params.get("num_batch", None)
                and payload["options"].get("num_batch") is None
            ):
                payload["options"]["num_batch"] = model_info.params.get(
                    "num_batch", None
                )

            if (
                model_info.params.get("num_keep", None)
                and payload["options"].get("num_keep") is None
            ):
                payload["options"]["num_keep"] = model_info.params.get("num_keep", None)

            if (
                model_info.params.get("repeat_last_n", None)
                and payload["options"].get("repeat_last_n") is None
            ):
                payload["options"]["repeat_last_n"] = model_info.params.get(
                    "repeat_last_n", None
                )

            if (
                model_info.params.get("frequency_penalty", None)
                and payload["options"].get("frequency_penalty") is None
            ):
                payload["options"]["repeat_penalty"] = model_info.params.get(
                    "frequency_penalty", None
                )

            if (
                model_info.params.get("temperature", None)
                and payload["options"].get("temperature") is None
            ):
                payload["options"]["temperature"] = model_info.params.get(
                    "temperature", None
                )

            if (
                model_info.params.get("seed", None)
                and payload["options"].get("seed") is None
            ):
                payload["options"]["seed"] = model_info.params.get("seed", None)

            if (
                model_info.params.get("stop", None)
                and payload["options"].get("stop") is None
            ):
                payload["options"]["stop"] = (
                    [
                        bytes(stop, "utf-8").decode("unicode_escape")
                        for stop in model_info.params["stop"]
                    ]
                    if model_info.params.get("stop", None)
                    else None
                )

            if (
                model_info.params.get("tfs_z", None)
                and payload["options"].get("tfs_z") is None
            ):
                payload["options"]["tfs_z"] = model_info.params.get("tfs_z", None)

            if (
                model_info.params.get("max_tokens", None)
                and payload["options"].get("max_tokens") is None
            ):
                payload["options"]["max_tokens"] = model_info.params.get(
                    "max_tokens", None
                )

            if (
                model_info.params.get("top_k", None)
                and payload["options"].get("top_k") is None
            ):
                payload["options"]["top_k"] = model_info.params.get("top_k", None)

            if (
                model_info.params.get("top_p", None)
                and payload["options"].get("top_p") is None
            ):
                payload["options"]["top_p"] = model_info.params.get("top_p", None)

            if (
                model_info.params.get("use_mmap", None)
                and payload["options"].get("use_mmap") is None
            ):
                payload["options"]["use_mmap"] = model_info.params.get("use_mmap", None)

            if (
                model_info.params.get("use_mlock", None)
                and payload["options"].get("use_mlock") is None
            ):
                payload["options"]["use_mlock"] = model_info.params.get(
                    "use_mlock", None
                )

            if (
                model_info.params.get("num_thread", None)
                and payload["options"].get("num_thread") is None
            ):
                payload["options"]["num_thread"] = model_info.params.get(
                    "num_thread", None
                )

        system = model_info.params.get("system", None)
        if system:
            system = prompt_template(
                system,
                **(
                    {
                        "user_name": user.name,
                        "user_location": (
                            user.info.get("location") if user.info else None
                        ),
                    }
                    if user
                    else {}
                ),
            )

            if payload.get("messages"):
                payload["messages"] = add_or_update_system_message(
                    system, payload["messages"]
                )
    if url_idx == None:
        if payload["model"] in app.state.MODELS:
            url_idx = random.choice(app.state.MODELS[payload["model"]]["urls"])
        else:
            raise HTTPException(
                status_code=400,
                detail=ERROR_MESSAGES.MODEL_NOT_FOUND(form_data.model),
            )
    aime_payload = get_aime_api_payload(payload)
    session = aiohttp.ClientSession(
        trust_env=True, timeout=aiohttp.ClientTimeout(total=AIOHTTP_CLIENT_TIMEOUT)
    )
    global model_api
    if model_api.endpoint_name != payload.get('model') or model_api.client_session_auth_key is None:
        model_api.endpoint_name = payload.get('model')

    await aime_api_login(url_idx)
    model_api.endpoint_name = payload.get('model')
    
    if payload.get('stream', False):
        aime_response_handler = AIMEResponseHandler(model_api.endpoint_name)
        response = asyncio.create_task(model_api.do_api_request_async(
            aime_payload, 
            progress_callback=aime_response_handler.progress_callback,
            result_callback=aime_response_handler.result_callback
        ))
        header = {
            'Content-Type': 'application/x-ndjson', 
            'Date': datetime.utcnow().strftime('%a, %d %b %Y %H:%M:%S GMT'), 
            'Transfer-Encoding': 'chunked'
        }
        return StreamingResponse(
            aime_response_handler.streaming_generator(), 
            headers=header, 
            background=BackgroundTask(
                cleanup_response, session=session
            )
        )
    else:
        response = await model_api.do_api_request_async(aime_payload)
        return JSONResponse(
            {
                'model': payload.get('model'),
                'created_at': time.time(),
                'choices': [
                    {
                        'index': 0, 
                        'message': {
                            'role': 'assistant',
                            'content': response.get('text')
                        },
                        'finish_reason': 'stop'
                    }
                ],
                'usage': {}
            }
        )
        
def get_aime_api_payload(payload):
    
    aime_payload = {'prompt_input': payload.get('messages').pop().get('content')} if payload.get('messages', [''])[-1].get('role') == 'user' else {}
    if not payload.get('messages'):
        payload['messages'] = [{
            'role': 'system',
            'content': ''
        }]
    aime_payload['chat_context'] = json.dumps(payload.get('messages'))

    valid_parameters = [ 'top_k', 'top_p', 'temperature', 'seed']
    for param_name, param_value in payload.get('options', {}).items():
        if param_name in valid_parameters:
            aime_payload[param_name] = param_value
    if payload.get('max_tokens') or payload.get('options', {}).get('max_tokens'):
        aime_payload['max_gen_tokens'] = payload.get('max_tokens') or payload.get('options', {}).get('max_tokens')
    return aime_payload