"""Точка входа Cloud Run Function.

Функция разворачивается из этого файла (--function=runs_api), поэтому здесь
остаётся только он. Логика разложена по слоям: config → domain / llm_prompt →
storage → api (#36).
"""
import functions_framework

from api import handle_request


@functions_framework.http
def runs_api(request):
    return handle_request(request)
