"""Legacy job advisor agent — preserved for reference.

This was the original single-agent approach. The new multi-agent
pipeline (CrawlAgent → ResumeAgent → MatchingAgent → RankingAgent)
replaces this. Kept for backward compatibility.
"""

from langchain.chat_models import init_chat_model
from langchain_tavily import TavilySearch
from langchain.agents import create_agent
import os

from dotenv import load_dotenv
load_dotenv()

# Web search tool
web_search = TavilySearch(max_results=5, topic="general")

# Multi-modal model
model = init_chat_model(
    model="qwen3.5-plus",
    model_provider="openai",
    base_url=os.getenv("DASHSCOPE_BASE_URL"),
    api_key=os.getenv("DASHSCOPE_API_KEY"),
)

# Agent system prompt
system_prompt = """
你是一名求职助手。收到用户的简历、技能描述或求职需求后，请严格按照以下流程操作：

1. 信息提取与理解：分析用户提供的文本或图片（如简历截图），提取关键信息：求职方向、核心技能、工作经验（年限/行业）、教育背景、期望岗位或行业。整理为"用户画像摘要"。

2. 市场信息检索：优先调用 web_search 工具，以"求职方向 + 岗位名称 + 招聘"等关键词，搜索当前市场上的相关职位、薪资范围、热门技能要求、行业趋势或招聘企业的需求。至少检索 3–5 条可靠信息（如招聘网站、行业报告）。

3. 匹配度评估与建议：对比用户画像与市场信息，从"技能匹配度"、"经验匹配度"、"竞争力提升空间"三个维度进行打分（满分10分）。然后给出具体建议：
   - 用户可以直接投递的职位类型及理由；
   - 用户需要补充或强化的技能/证书/项目经验；
   - 推荐的学习资源（课程、书籍、网站）或求职渠道（招聘平台、内推建议）。

4. 结构化输出：生成一份清晰、友好的求职建议报告，包含以下部分：
   - 用户画像摘要
   - 市场机会概览（岗位、薪资、技能需求）
   - 匹配度评分（分维度）
   - 具体行动建议（分短期和长期）
   - 参考链接或进一步帮助的引导

注意：优先使用 web_search 获取实时招聘信息，只有在搜索失败或明显无结果时，才根据自己的知识提供建议。回答尽量使用中文，语气专业且鼓励。
"""

# Create Agent
agent = create_agent(
    model=model,
    tools=[web_search],
    system_prompt=system_prompt,
)
