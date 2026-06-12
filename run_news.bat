@echo off
cd /d D:\personal_news_assistant
if not exist logs mkdir logs
set WRAPPER_LOG=logs\wrapper.log

>> "%WRAPPER_LOG%" echo ==================================================
>> "%WRAPPER_LOG%" echo RUN START %DATE% %TIME%
>> "%WRAPPER_LOG%" echo Current working directory: %CD%
>> "%WRAPPER_LOG%" echo Username: %USERNAME%
>> "%WRAPPER_LOG%" echo where uv:
where uv >> "%WRAPPER_LOG%" 2>&1
>> "%WRAPPER_LOG%" echo python executable:
uv run python -c "import sys; print(sys.executable)" >> "%WRAPPER_LOG%" 2>&1
>> "%WRAPPER_LOG%" echo main.py start %DATE% %TIME%

uv run python main.py
set MAIN_EXIT_CODE=%ERRORLEVEL%

>> "%WRAPPER_LOG%" echo main.py end %DATE% %TIME%
>> "%WRAPPER_LOG%" echo main.py exit code: %MAIN_EXIT_CODE%
>> "%WRAPPER_LOG%" echo RUN END %DATE% %TIME%

exit /b %MAIN_EXIT_CODE%
