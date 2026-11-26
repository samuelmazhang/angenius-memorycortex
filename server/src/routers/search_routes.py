"""Route declarations for the ``/search`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/search', H['search_memory'], methods=['POST'])
    router.add_api_route('/search/tts', H['search_tts'], methods=['GET'])
    router.add_api_route('/search/hybrid', H['search_hybrid'], methods=['GET'])
