import os
import logging
import asyncio
from typing import Any, Coroutine, List, Literal, Optional, Union, overload
from azure.search.documents.aio import SearchClient
from azure.search.documents.models import VectorQuery
from openai import AsyncOpenAI, AsyncStream
from openai.types.chat import (
    ChatCompletion,
    ChatCompletionChunk,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from openai_messages_token_helper import build_messages, get_token_limit
from approaches.approach import ThoughtStep
from approaches.chatapproach import ChatApproach
from core.authentication import AuthenticationHelper
from semantic_kernel import Kernel
from semantic_kernel.connectors.ai.open_ai import AzureChatCompletion, OpenAIChatCompletion
from semantic_kernel.connectors.ai.function_call_behavior import FunctionCallBehavior
from semantic_kernel.connectors.ai.chat_completion_client_base import ChatCompletionClientBase
from semantic_kernel.contents.chat_history import ChatHistory
from semantic_kernel.functions.kernel_arguments import KernelArguments
from semantic_kernel.connectors.ai.open_ai.prompt_execution_settings.azure_chat_prompt_execution_settings import (
    AzureChatPromptExecutionSettings, OpenAIChatPromptExecutionSettings
)
#from services import Service
#from samples.service_settings import ServiceSettings
from semantic_kernel.planners.function_calling_stepwise_planner import (
    FunctionCallingStepwisePlanner,
    FunctionCallingStepwisePlannerOptions,
)


class ChatReadRetrieveReadApproach(ChatApproach):
    """
    A multi-step approach that first uses OpenAI to turn the user's question into a search query,
    then uses Azure AI Search to retrieve relevant documents, and then sends the conversation history,
    original user question, and search results to OpenAI to generate a response.
    """

    def __init__(
        self,
        *,
        search_client: SearchClient,
        auth_helper: AuthenticationHelper,
        openai_client: AsyncOpenAI,
        chatgpt_model: str,
        chatgpt_deployment: Optional[str],  # Not needed for non-Azure OpenAI
        embedding_deployment: Optional[str],  # Not needed for non-Azure OpenAI or for retrieval_mode="text"
        embedding_model: str,
        embedding_dimensions: int,
        sourcepage_field: str,
        content_field: str,
        query_language: str,
        query_speller: str,
    ):
        self.search_client = search_client
        self.openai_client = openai_client
        self.auth_helper = auth_helper
        self.chatgpt_model = chatgpt_model
        self.chatgpt_deployment = chatgpt_deployment
        self.embedding_deployment = embedding_deployment
        self.embedding_model = embedding_model
        self.embedding_dimensions = embedding_dimensions
        self.sourcepage_field = sourcepage_field
        self.content_field = content_field
        self.query_language = query_language
        self.query_speller = query_speller
        self.chatgpt_token_limit = get_token_limit(chatgpt_model)

    @property
    def system_message_chat_conversation(self):
        return """You are an advanced AI assistant specializing in financial regulations and compliance. Your role is to provide accurate, concise guidance based on official regulatory sources. Approach each query as a knowledgeable regulatory advisor would.

                Key instructions:
                1. Answer ONLY using facts from the provided sources below. If information is insufficient, state that you don't have enough information to provide a complete answer.
                2. Do not generate answers without referencing the given sources.
                3. Be concise yet thorough in your responses, prioritizing clarity and accuracy.
                4. If a clarifying question would help, ask it briefly and professionally.
                5. Present tabular information in HTML format, not markdown.
                6. If the question is in a language other than English, respond in that language.

                Source citation:
                - Each source has a name followed by a colon and the actual information.
                - Always include the source name for each fact used in your response.
                - Use square brackets to reference sources, e.g., [info1.txt].
                - List sources separately, e.g., [info1.txt][info2.pdf]. Do not combine sources.

                Regulatory focus:
                - Interpret regulations with a focus on organizational compliance and risk management.
                - Highlight key compliance requirements, potential risks, and best practices.
                - When relevant, briefly mention implications for governance, reporting, or audit processes.
                - Address any apparent regulatory gaps or areas needing clarification, if applicable.

                {follow_up_questions_prompt}
                {injected_prompt}
                """

    @overload
    async def run_until_final_call(
        self,
        messages: list[ChatCompletionMessageParam],
        overrides: dict[str, Any],
        auth_claims: dict[str, Any],
        should_stream: Literal[False],
    ) -> tuple[dict[str, Any], Coroutine[Any, Any, ChatCompletion]]: ...

    @overload
    async def run_until_final_call(
        self,
        messages: list[ChatCompletionMessageParam],
        overrides: dict[str, Any],
        auth_claims: dict[str, Any],
        should_stream: Literal[True],
    ) -> tuple[dict[str, Any], Coroutine[Any, Any, AsyncStream[ChatCompletionChunk]]]: ...
    
    
    async def run_until_final_call(
        self,
        messages: list[ChatCompletionMessageParam],
        overrides: dict[str, Any],
        auth_claims: dict[str, Any],
        should_stream: bool = False,
    ) -> tuple[dict[str, Any], Coroutine[Any, Any, Union[ChatCompletion, AsyncStream[ChatCompletionChunk]]]]:
        use_text_search = overrides.get("retrieval_mode") in ["text", "hybrid", None]
        use_vector_search = overrides.get("retrieval_mode") in ["vectors", "hybrid", None]
        use_semantic_ranker = True if overrides.get("semantic_ranker") else False
        use_semantic_captions = True if overrides.get("semantic_captions") else False
        top = overrides.get("top", 3)
        minimum_search_score = overrides.get("minimum_search_score", 0.0)
        minimum_reranker_score = overrides.get("minimum_reranker_score", 0.0)
        filter = self.build_filter(overrides, auth_claims)
            
        original_user_query = messages[-1]["content"]
        if not isinstance(original_user_query, str):
            raise ValueError("The most recent message content must be a string.")
        user_query_request = "Generate search query for: " + original_user_query

        tools: List[ChatCompletionToolParam] = [
            {
                "type": "function",
                "function": {
                    "name": "search_sources",
                    "description": "Retrieve sources from the Azure AI Search index",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "search_query": {
                                "type": "string",
                                "description": "Query string to retrieve documents from azure search eg: 'Health care plan'",
                            }
                        },
                        "required": ["search_query"],
                    },
                },
            }
        ]

        # STEP 1: Generate an optimized keyword search query based on the chat history and the last question
        query_response_token_limit = 100
        query_messages = build_messages(
            model=self.chatgpt_model,
            system_prompt=self.query_prompt_template,
            tools=tools,
            few_shots=self.query_prompt_few_shots,
            past_messages=messages[:-1],
            new_user_content=user_query_request,
            max_tokens=self.chatgpt_token_limit - query_response_token_limit,
        )
        chat_completion: ChatCompletion = await self.openai_client.chat.completions.create(
            messages=query_messages,  # type: ignore
            # Azure OpenAI takes the deployment name as the model name
            model=self.chatgpt_deployment if self.chatgpt_deployment else self.chatgpt_model,
            temperature=0.0,  # Minimize creativity for search query generation
            max_tokens=query_response_token_limit,  # Setting too low risks malformed JSON, setting too high may affect performance
            n=1,
            tools=tools,
        )
        
        query_text = self.get_search_query(chat_completion, original_user_query)
        
        # STEP 2: Retrieve relevant documents from the search index with the GPT optimized query

        # If retrieval mode includes vectors, compute an embedding for the query
        vectors: list[VectorQuery] = []
        if use_vector_search:
            vectors.append(await self.compute_text_embedding(query_text))

        results = await self.search(
            top,
            query_text,
            filter,
            vectors,
            use_text_search,
            use_vector_search,
            use_semantic_ranker,
            use_semantic_captions,
            minimum_search_score,
            minimum_reranker_score,
        )

        sources_content = self.get_sources_content(results, use_semantic_captions, use_image_citation=False)
        content = "\n".join(sources_content)

        # STEP 3: Generate a contextual and content specific answer using the search results and chat history

        # Allow client to replace the entire prompt, or to inject into the exiting prompt using >>>
        system_message = self.get_system_prompt(
            overrides.get("prompt_template"),
            self.follow_up_questions_prompt_content if overrides.get("suggest_followup_questions") else "",
        )

        response_token_limit = 1024
        
        messages = build_messages(
            model=self.chatgpt_model,
            system_prompt=system_message,
            past_messages=messages[:-1],
            new_user_content=(
            f"{original_user_query}\n\n"
            f"Additional Sources:\n{content}"
        ),
            max_tokens=self.chatgpt_token_limit - response_token_limit,
        )

        data_points = {"text": sources_content}

        extra_info = {
            "data_points": data_points,
            "thoughts": [
                ThoughtStep(
                    "Prompt to generate search query",
                    [str(message) for message in query_messages],
                    (
                        {"model": self.chatgpt_model, "deployment": self.chatgpt_deployment}
                        if self.chatgpt_deployment
                        else {"model": self.chatgpt_model}
                    ),
                ),
                ThoughtStep(
                    "Search using generated search query",
                    query_text,
                    {
                        "use_semantic_captions": use_semantic_captions,
                        "use_semantic_ranker": use_semantic_ranker,
                        "top": top,
                        "filter": filter,
                        "use_vector_search": use_vector_search,
                        "use_text_search": use_text_search,
                    },
                ),
                ThoughtStep(
                    "Search results",
                    [result.serialize_for_results() for result in results],
                ),
                ThoughtStep(
                    "Prompt to generate answer",
                    [str(message) for message in messages],
                    (
                        {"model": self.chatgpt_model, "deployment": self.chatgpt_deployment}
                        if self.chatgpt_deployment
                        else {"model": self.chatgpt_model}
                    ),
                ),
            ],
        }

        chat_coroutine = self.openai_client.chat.completions.create(
            # Azure OpenAI takes the deployment name as the model name
            model=self.chatgpt_deployment if self.chatgpt_deployment else self.chatgpt_model,
            messages=messages,
            temperature=overrides.get("temperature", 0.3),
            max_tokens=response_token_limit,
            n=1,
            stream=should_stream,
        )
        return (extra_info, chat_coroutine)