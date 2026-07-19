# 🚀 部署指南 — Job Matching AI Agent

将后端 API 和前端页面部署上线。

---

## 目录

1. [后端部署 (FastAPI)](#1-后端部署)
2. [前端部署 (React)](#2-前端部署)
3. [集成到 lingdesign.top](#3-集成到个人网站)

---

## 1. 后端部署

### 方案 A：Railway（推荐）

1. 注册 [Railway](https://railway.app)（支持 GitHub 登录）
2. 安装 CLI：`npm i -g @railway/cli`
3. 在项目目录登录：
   ```bash
   railway login
   ```
4. 部署：
   ```bash
   railway up
   ```
5. 在 Railway Dashboard 设置环境变量（从 `.env` 复制）：
   - `DASHSCOPE_API_KEY`
   - `DASHSCOPE_BASE_URL`
   - `LLM_MODEL`
   - 其他需要的变量

### 方案 B：Render

1. Fork 或推送代码到 GitHub
2. 在 [Render Dashboard](https://dashboard.render.com) → New Web Service
3. 连接你的 GitHub 仓库
4. 设置：
   - **Runtime**: Python
   - **Build Command**: `pip install -r requirements.txt && python -m playwright install chromium --with-deps || true`
   - **Start Command**: `uvicorn app.api.server:app --host 0.0.0.0 --port $PORT`
   - **Health Path**: `/health`
5. 添加环境变量（同 Railway）

### 方案 C：阿里云 ECS / VPS

```bash
# 在服务器上
git clone https://github.com/yourusername/work_agent.git
cd work_agent
pip install -r requirements.txt
python -m playwright install chromium --with-deps

# 使用 PM2 或 systemd 保持运行
pip install pm2 或者使用 systemd 服务
uvicorn app.api.server:app --host 0.0.0.0 --port 8000
```

### 方案 D：Docker

```bash
docker build -t job-matching-agent .
docker run -d --name job-matching -p 8000:8000 --env-file .env job-matching-agent
```

---

## 2. 前端 — LiveDemo.tsx 单文件组件

```bash
# 前端已从 work_agent 仓库移至个人网站仓库：
# ling-web/src/components/LiveDemo.tsx
```

### 使用方法

将 `LiveDemo.tsx` 放入 [ling-web](https://github.com/mling/ling-web) 的 `src/components/`，然后在页面中引用：

```tsx
import LiveDemo from './components/LiveDemo'

// 在路由或页面中
<LiveDemo apiBase="https://your-backend.railway.app" />
```

### 依赖

确保 ling-web 已安装：

```bash
npm install lucide-react
```

Tailwind CSS v4 需要配置 `@tailwindcss/vite`（如已有则无需额外操作）。

### 开发模式（Vite proxy）

```tsx
<LiveDemo apiBase="/api" />
```

在 `vite.config.js` 中添加 proxy：

```js
server: {
  proxy: { '/api': { target: 'http://localhost:8000', changeOrigin: true } },
}
```

### 构建与部署

```bash
# 在 ling-web 中正常构建
npm run build
# LiveDemo.tsx 会自动包含在产物中
```

---

## 3. 集成到个人网站

### 方式 A：iframe 嵌入（最简单）

在 lingdesign.top 的 `/project/job-matching-ai-agent` 页面中添加：

```html
<iframe 
  src="https://job-matching-frontend.vercel.app" 
  style="width: 100%; height: 800px; border: none; border-radius: 12px;"
  title="Job Matching AI Agent Demo"
/>
```

### 方式 B：直接集成到 React 代码

将 `frontend/src/` 下的组件复制到你的网站项目中：

```jsx
// 在你的路由配置中添加
import ProjectDemo from './pages/ProjectDemo';

<Route path="/project/job-matching-ai-agent" element={<ProjectDemo />} />
```

需要安装的依赖：
```bash
npm install tailwindcss @tailwindcss/vite react-router-dom lucide-react
```

### 方式 C：独立页面 + 导航链接

在网站顶部导航添加链接指向独立部署的前端页面：

```html
<a href="https://job-matching-frontend.vercel.app">AI Job Matching</a>
```

---

## 4. 验证部署

后端健康检查：

```bash
curl https://your-backend-url.up.railway.app/health
# 返回: {"status":"ok","service":"job-matching-ai-agent","version":"1.0.0"}
```
