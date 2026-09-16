@echo off
setlocal

REM ============================================================
REM  一键启动脚本：同时拉起后端和前端
REM  用法：双击本文件
REM
REM  ------------------------------------------------------------
REM  关于编码（这一段是踩过坑才加的，请不要删、也不要改成 UTF-8）
REM  ------------------------------------------------------------
REM  本文件必须保存成 GBK/ANSI 编码，并且**不能在文件中间切换代码页**。
REM
REM  之前把它存成 UTF-8 并在开头写了 chcp 65001，结果双击后满屏都是
REM      'o' 不是内部或外部命令
REM      'cho' 不是内部或外部命令
REM  这样的报错，服务一个都起不来，浏览器自然也打不开。
REM
REM  原因是 cmd.exe 按**字节位置**读取批处理文件。文件里全中文（多字节），
REM  cmd 一开始按系统默认的 GBK 去解码，中途 chcp 换成 UTF-8 之后，
REM  它记录的位置和实际字节位置就对不上了，于是读串行，
REM  把行中间的字符（echo 的 o、ch 之类）当成命令去执行。
REM
REM  中文版 Windows 的控制台本身就是 GBK，所以直接用 GBK 存文件，
REM  中文就能正常显示，完全不需要 chcp。
REM
REM  但后端 Python 的日志是按 UTF-8 输出的（见 backend/app/main.py），
REM  所以要在**每个新打开的窗口内部**单独设一次 chcp 65001。
REM  那是另一个进程，不会影响本文件的解析。
REM ============================================================

cd /d "%~dp0"

echo.
echo ============================================================
echo   电商知识库问答系统 - 启动中
echo ============================================================
echo.

REM ---------- 检查后端虚拟环境 ----------
if not exist "backend\.venv\Scripts\python.exe" (
    echo [错误] 没找到后端虚拟环境，请先在 backend 目录下执行：
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

REM ---------- 检查配置文件 ----------
if not exist "backend\.env" (
    echo [提示] 没找到 backend\.env，正在从模板复制一份...
    copy "backend\.env.example" "backend\.env" > nul
    echo.
    echo ************************************************************
    echo   请打开 backend\.env，把 DASHSCOPE_API_KEY 填成你自己的
    echo   百炼 API Key，否则文档向量化和问答功能无法使用。
    echo   获取地址：https://bailian.console.aliyun.com/
    echo ************************************************************
    echo.
    pause
)

REM ---------- 检查前端依赖 ----------
if not exist "frontend\node_modules" (
    echo [提示] 前端依赖还没装，正在安装（首次需要几分钟）...
    pushd frontend
    call npm install
    popd
    echo.
)

REM ---------- 检查端口有没有被上一次没关干净的进程占着 ----------
REM 这种情况的表现是"启动看着成功了，但浏览器就是打不开"，
REM 很难想到是旧进程没退，所以这里主动提示一下
netstat -ano | findstr ":8000" | findstr "LISTENING" > nul
if not errorlevel 1 (
    echo [警告] 8000 端口已经被占用了，多半是上次的后端没关干净。
    echo        浏览器如果打不开，请打开任务管理器结束 python.exe 再重试。
    echo.
)
netstat -ano | findstr ":5173" | findstr "LISTENING" > nul
if not errorlevel 1 (
    echo [警告] 5173 端口已经被占用了，多半是上次的前端没关干净。
    echo        浏览器如果打不开，请打开任务管理器结束 node.exe 再重试。
    echo.
)

REM ---------- 启动后端 ----------
echo [1/2] 启动后端服务（端口 8000）...
start "RAG后端" cmd /k "chcp 65001 >nul && cd /d "%~dp0backend" && .venv\Scripts\python.exe -m uvicorn app.main:app --port 8000"

REM 等后端把端口起起来，避免前端首屏请求失败
timeout /t 6 /nobreak > nul

REM ---------- 启动前端 ----------
echo [2/2] 启动前端服务（端口 5173）...
start "RAG前端" cmd /k "chcp 65001 >nul && cd /d "%~dp0frontend" && npm run dev"

timeout /t 8 /nobreak > nul

echo.
echo ============================================================
echo   启动完成
echo ------------------------------------------------------------
echo   前端页面：http://localhost:5173
echo   接口文档：http://127.0.0.1:8000/docs
echo.
echo   管理员账号：admin / 123456
echo   普通用户请在前端页面自行注册
echo ------------------------------------------------------------
echo   需要停止服务时，请关掉那两个新开的窗口
echo ============================================================
echo.

start http://localhost:5173
pause
