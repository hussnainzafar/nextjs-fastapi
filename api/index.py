"""
Anablock Crawler Service
--------------------------

A FastAPI-based web crawler service that processes web pages and stores their content
in Upstash Vector store for semantic search capabilities.

Features:
- Asynchronous web crawling
- Vector storage integration
- Job status tracking
- Configurable crawl depth and patterns
- Error handling and retry mechanisms

Environment Variables Required:
    OPENAI_API_KEY: OpenAI API key for embeddings
    UPSTASH_VECTOR_REST_URL: Upstash Vector REST API URL
    UPSTASH_VECTOR_REST_TOKEN: Upstash Vector REST API token
    UPSTASH_REDIS_REST_URL: Upstash Redis REST API URL
    UPSTASH_REDIS_REST_TOKEN: Upstash Redis REST API token
"""

from fastapi import (
    FastAPI,
    BackgroundTasks,
    HTTPException,
    WebSocket,
    WebSocketDisconnect
)
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, validator
import os
import uuid
import logging
import json
from typing import List, Optional, Dict, Set
from langchain.text_splitter import RecursiveCharacterTextSplitter
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import yaml
from openai import OpenAI
from upstash_vector import Index
from urllib.parse import urljoin, urlparse
from datetime import datetime
from enum import Enum
import asyncio

# Load environment variables
load_dotenv()

# Add this near the top of your file after imports
print("NODE_ENV:", os.environ.get("NODE_ENV", "not set"))
print("VERCEL_ENV:", os.environ.get("VERCEL_ENV", "not set"))

app = FastAPI(
    docs_url="/api/py/docs",
    openapi_url="/api/py/openapi.json",
    title="Anablock Crawler",
    description="Web crawler service with vector storage capabilities",
    version="1.0.0"
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Update the config path to use os.path for reliable path resolution
import os

# Assuming the YAML is in the 'api/utils' directory relative to this file
current_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(current_dir, "utils", "crawler.yaml")

with open(config_path, 'r') as file:
    config = yaml.load(file, Loader=yaml.FullLoader)

crawler_config = config["crawler"]
text_splitter_config = config["index"]["text_splitter"]

class UpstashVectorStore:
    """
    Interface to Upstash Vector store for managing embeddings.

    Attributes:
        client (OpenAI): OpenAI client for generating embeddings
        index (Index): Upstash Vector index instance
    """

    def __init__(self, url: str, token: str):
        """
        Initialize vector store connection.

        Args:
            url (str): Upstash Vector REST API URL
            token (str): Upstash Vector REST API token
        """
        self.client = OpenAI(
            api_key=os.environ.get("OPENAI_API_KEY")
        )
        self.index = Index(url=url, token=token)

    def get_embeddings(self, documents: List[str], model: str = "text-embedding-ada-002") -> List[List[float]]:
        try:
            documents = [document.replace("\n", " ") for document in documents]
            embeddings = self.client.embeddings.create(
                input=documents,
                model=model
            )
            return [data.embedding for data in embeddings.data]
        except Exception as e:
            print(f"Error getting embeddings: {str(e)}")
            raise

    def add(self, ids: List[str], documents: List[str], link: str) -> None:
        """
        Add documents to vector store.

        Args:
            ids (List[str]): List of unique identifiers
            documents (List[str]): List of text documents
            link (str): Source URL of the documents
        """
        try:
            embeddings = self.get_embeddings(documents)
            self.index.upsert(
                vectors=[
                    (id, embedding, {"text": document, "url": link})
                    for id, embedding, document in zip(ids, embeddings, documents)
                ]
            )
        except Exception as e:
            print(f"Error adding to vector store: {str(e)}")
            raise

    async def get_stats(self) -> dict:
        """
        Get vector store statistics.

        Returns:
            dict: Statistics about the vector store
        """
        try:
            info = self.index.info()
            return {
                "vectors": info.vectors,
                "dimension": info.dimension,
                "size": info.size,
                "name": info.name,
                "similarity": info.similarity,
                "status": info.status
            }
        except Exception as e:
            print(f"Error getting vector store stats: {str(e)}")
            return {"error": str(e)}

class CrawlRequest(BaseModel):
    """
    Request model for crawl operations.

    Attributes:
        urls (List[str]): List of URLs to crawl
        use_vector_store (bool): Whether to store results in vector store
        recursive (bool): Whether to follow links recursively
        max_depth (int): Maximum depth for recursive crawling
        allowed_domains (Optional[List[str]]): List of allowed domains
        exclude_patterns (Optional[List[str]]): Patterns to exclude from crawling
    """
    urls: List[str]
    use_vector_store: bool = True
    recursive: bool = False
    max_depth: int = 2
    allowed_domains: Optional[List[str]] = None
    exclude_patterns: Optional[List[str]] = None

    @validator('urls')
    def validate_urls(cls, v):
        """Validate that at least one URL is provided."""
        if not v:
            raise ValueError("At least one URL is required")
        return v

class CrawlResponse(BaseModel):
    """
    Response model for crawl operations.

    Attributes:
        task_id (str): Unique identifier for the crawl task
        status (str): Status of the crawl request
        message (str): Human-readable status message
    """
    task_id: str
    status: str
    message: str

class JobStatus(str, Enum):
    """Enumeration of possible job statuses."""
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"

class JobManager:
    """
    Manages job status tracking using Upstash Redis.

    Attributes:
        upstash_url (str): Upstash Redis REST API URL
        upstash_token (str): Upstash Redis REST API token
        client (httpx.AsyncClient): Async HTTP client for API requests
        active_connections (Dict[str, Set[WebSocket]]): Set of active WebSocket connections
    """

    def __init__(self):
        """Initialize job manager with HTTP client."""
        self.upstash_url = os.getenv("UPSTASH_REDIS_REST_URL")
        self.upstash_token = os.getenv("UPSTASH_REDIS_REST_TOKEN")
        self.active_connections: Dict[str, Set[WebSocket]] = {}
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
            verify=False  # Only if needed
        )

    async def _make_request(self, method: str, endpoint: str, data: dict = None) -> dict:
        """
        Make HTTP request to Upstash Redis.

        Args:
            method (str): HTTP method (GET or POST)
            endpoint (str): API endpoint
            data (dict, optional): Request data for POST requests

        Returns:
            dict: Response data
        """
        try:
            headers = {
                "Authorization": f"Bearer {self.upstash_token}"
            }
            
            if method == "GET":
                response = await self.client.get(
                    f"{self.upstash_url}{endpoint}",
                    headers=headers
                )
            elif method == "POST":
                response = await self.client.post(
                    f"{self.upstash_url}{endpoint}",
                    headers=headers,
                    json=data
                )
            
            response.raise_for_status()
            return response.json()
            
        except httpx.TimeoutException:
            print(f"Timeout while making {method} request to {endpoint}")
            return {"error": "Request timeout"}
        except Exception as e:
            print(f"Error in _make_request: {str(e)}")
            return {"error": str(e)}

    async def connect_client(self, task_id: str, websocket: WebSocket):
        """Register a WebSocket connection for a task."""
        await websocket.accept()
        if task_id not in self.active_connections:
            self.active_connections[task_id] = set()
        self.active_connections[task_id].add(websocket)

    async def disconnect_client(self, task_id: str, websocket: WebSocket):
        """Remove a WebSocket connection."""
        if task_id in self.active_connections:
            self.active_connections[task_id].remove(websocket)
            if not self.active_connections[task_id]:
                del self.active_connections[task_id]

    async def notify_clients(self, task_id: str, data: dict):
        """Send update to all connected clients for a task."""
        if task_id in self.active_connections:
            dead_connections = set()
            for connection in self.active_connections[task_id]:
                try:
                    await connection.send_json(data)
                except WebSocketDisconnect:
                    dead_connections.add(connection)
            
            # Clean up dead connections
            for dead in dead_connections:
                await self.disconnect_client(task_id, dead)

    async def set_job_status(self, task_id: str, status: JobStatus, details: dict = None):
        """Update job status and notify connected clients."""
        try:
            job_info = {
                "status": status,
                "updated_at": datetime.utcnow().isoformat(),
                "details": details or {}
            }
            key = f"job:{task_id}"
            
            # Store in Redis
            await self._make_request(
                "POST",
                f"/set/{key}",
                {"value": json.dumps(job_info)}
            )
            
            # Notify WebSocket clients
            await self.notify_clients(task_id, job_info)
            
        except Exception as e:
            print(f"Error setting job status: {str(e)}")

    async def get_job_status(self, task_id: str):
        """
        Get current job status from Redis.

        Args:
            task_id (str): Unique task identifier

        Returns:
            Optional[dict]: Job status information if found
        """
        try:
            key = f"job:{task_id}"
            response = await self._make_request("GET", f"/get/{key}")
            
            if response.get("error"):
                return None
                
            if response.get("result"):
                return json.loads(response["result"])
                
            return None
            
        except Exception as e:
            print(f"Error getting job status: {str(e)}")
            return None

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.client.aclose()

# Initialize JobManager as a global variable
job_manager = JobManager()

@app.on_event("startup")
async def startup_event():
    """Initialize services on application startup."""
    # Initialize the HTTP client
    job_manager.client = httpx.AsyncClient(
        timeout=httpx.Timeout(10.0),
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=20),
        verify=False  # Only if needed
    )

@app.on_event("shutdown")
async def shutdown_event():
    """Cleanup on application shutdown."""
    # Close the HTTP client
    await job_manager.client.aclose()

def disable_loggers():
    disable_loggers = [
        "httpcore.http11",
        "httpx",
        "openai._base_client",
        "urllib3.connectionpool"
    ]
    for logger in disable_loggers:
        logging.getLogger(logger).setLevel(logging.WARNING)

async def is_valid_url(url: str, allowed_domains: Optional[List[str]], exclude_patterns: Optional[List[str]]) -> bool:
    """Check if URL should be crawled based on domain and pattern restrictions."""
    if allowed_domains:
        domain = urlparse(url).netloc
        if domain not in allowed_domains:
            return False
    
    if exclude_patterns:
        for pattern in exclude_patterns:
            if pattern in url:
                return False
    
    return True

async def extract_links(soup: BeautifulSoup, base_url: str) -> List[str]:
    """Extract all links from a page."""
    links = []
    for a_tag in soup.find_all('a', href=True):
        href = a_tag['href']
        full_url = urljoin(base_url, href)
        if full_url.startswith(('http://', 'https://')):
            links.append(full_url)
    return links

async def process_page(
    url: str,
    text_splitter: RecursiveCharacterTextSplitter,
    task_id: str,
    vector_store: Optional[UpstashVectorStore] = None,
    recursive: bool = False,
    current_depth: int = 0,
    max_depth: int = 2,
    allowed_domains: Optional[List[str]] = None,
    exclude_patterns: Optional[List[str]] = None,
    visited_urls: Optional[set] = None
) -> None:
    """Process a single page with improved error handling."""
    if visited_urls is None:
        visited_urls = set()
    
    try:
        if url in visited_urls:
            return
        
        visited_urls.add(url)
        
        # Update initial status
        await job_manager.set_job_status(
            task_id,
            JobStatus.RUNNING,
            {
                "current_url": url,
                "depth": current_depth,
                "total_urls": len(visited_urls),
                "status": "Processing page"
            }
        )
        
        if current_depth > max_depth:
            return
        
        if not await is_valid_url(url, allowed_domains, exclude_patterns):
            return

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            verify=False  # Only if needed
        ) as client:
            try:
                response = await client.get(url)
                response.raise_for_status()
                
                # Process content and update vector store
                soup = BeautifulSoup(response.text, 'html.parser')
                paragraphs = soup.find_all('p')
                text_content = '\n'.join([p.get_text() for p in paragraphs])
                
                if not text_content.strip():
                    return
                
                documents = text_splitter.split_text(text_content)
                
                if documents and vector_store:
                    doc_ids = [str(uuid.uuid4())[:8] for _ in documents]
                    vector_store.add(
                        ids=doc_ids,
                        documents=documents,
                        link=url
                    )
                    
                    # Handle recursive crawling
                    if recursive and current_depth < max_depth:
                        links = await extract_links(soup, url)
                        for link in links:
                            await process_page(
                                url=link,
                                text_splitter=text_splitter,
                                task_id=task_id,
                                vector_store=vector_store,
                                recursive=recursive,
                                current_depth=current_depth + 1,
                                max_depth=max_depth,
                                allowed_domains=allowed_domains,
                                exclude_patterns=exclude_patterns,
                                visited_urls=visited_urls
                            )
                
            except httpx.TimeoutException:
                print(f"Timeout while processing {url}")
                return
            except Exception as e:
                print(f"Error processing {url}: {str(e)}")
                return

    except Exception as e:
        await job_manager.set_job_status(
            task_id,
            JobStatus.FAILED,
            {
                "error": str(e),
                "current_depth": current_depth,
                "total_urls_processed": len(visited_urls)
            }
        )
        return

    # Update final status
    if current_depth == 0:
        await job_manager.set_job_status(
            task_id,
            JobStatus.COMPLETED,
            {
                "total_urls_processed": len(visited_urls),
                "max_depth_reached": current_depth,
                "final_status": "Crawl completed successfully"
            }
        )

@app.get("/api/py/helloFastApi")
def hello_fast_api():
    return {"message": "Hello from FastAPI"}

@app.post("/api/py/crawl", response_model=CrawlResponse)
async def start_crawl(crawl_request: CrawlRequest, background_tasks: BackgroundTasks):
    """Start a new crawling task."""
    try:
        if not crawl_request.use_vector_store:
            raise HTTPException(
                status_code=400,
                detail="Vector store must be enabled for crawling"
            )

        task_id = str(uuid.uuid4())
        
        # Initialize vector store
        try:
            vector_store = UpstashVectorStore(
                url=os.environ.get("UPSTASH_VECTOR_REST_URL"),
                token=os.environ.get("UPSTASH_VECTOR_REST_TOKEN")
            )
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to initialize vector store: {str(e)}"
            )

        # Initialize job status
        await job_manager.set_job_status(
            task_id,
            JobStatus.PENDING,
            {
                "urls": [str(url) for url in crawl_request.urls],
                "recursive": crawl_request.recursive,
                "max_depth": crawl_request.max_depth,
                "status": "Initializing crawl"
            }
        )

        text_splitter = RecursiveCharacterTextSplitter(**text_splitter_config)
        visited_urls = set()
        
        for url in crawl_request.urls:
            background_tasks.add_task(
                process_page,
                str(url),
                text_splitter,
                task_id,
                vector_store,
                crawl_request.recursive,
                0,
                crawl_request.max_depth,
                crawl_request.allowed_domains,
                crawl_request.exclude_patterns,
                visited_urls
            )
        
        return CrawlResponse(
            task_id=task_id,
            status="pending",
            message=f"Crawl initiated for {len(crawl_request.urls)} URLs"
        )

    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to start crawl: {str(e)}"
        )

@app.get("/api/py/vector-store/info")
async def get_vector_store_info():
    try:
        vector_store = UpstashVectorStore(
            url=os.environ.get("UPSTASH_VECTOR_REST_URL"),
            token=os.environ.get("UPSTASH_VECTOR_REST_TOKEN")
        )
        info = vector_store.index.info()
        return {
            "status": "success",
            "info": info
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error accessing vector store: {str(e)}"
        }

@app.get("/api/py/vector-store/search")
async def search_vector_store(query: str, limit: int = 5):
    try:
        vector_store = UpstashVectorStore(
            url=os.environ.get("UPSTASH_VECTOR_REST_URL"),
            token=os.environ.get("UPSTASH_VECTOR_REST_TOKEN")
        )
        
        # Get embedding for the query
        query_embedding = vector_store.get_embeddings([query])[0]
        
        # Search the vector store
        results = vector_store.index.query(
            vector=query_embedding,
            top_k=limit,
            include_metadata=True
        )
        
        return {
            "status": "success",
            "query": query,
            "results": results
        }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Error searching vector store: {str(e)}"
        }

# Add WebSocket endpoint for real-time status updates
@app.websocket("/api/py/ws/{task_id}")
async def websocket_endpoint(websocket: WebSocket, task_id: str):
    """WebSocket endpoint for real-time status updates."""
    try:
        await job_manager.connect_client(task_id, websocket)
        
        # Send current status immediately after connection
        current_status = await job_manager.get_job_status(task_id)
        if current_status:
            await websocket.send_json(current_status)
        
        while True:
            # Keep connection alive
            await websocket.receive_text()
            
    except WebSocketDisconnect:
        await job_manager.disconnect_client(task_id, websocket)

# Add status check endpoint
@app.get("/api/py/status/{task_id}")
async def get_job_status(task_id: str):
    status = await job_manager.get_job_status(task_id)
    if not status:
        raise HTTPException(
            status_code=404,
            detail=f"No status found for task ID: {task_id}"
        )
    return status

# Or add this to your existing endpoints for testing
@app.get("/api/py/env-check")
async def check_environment():
    return {
        "NODE_ENV": os.environ.get("NODE_ENV", "not set"),
        "VERCEL_ENV": os.environ.get("VERCEL_ENV", "not set"),
        "IS_VERCEL": os.environ.get("VERCEL", "not set")
    }
