"""Route declarations for the ``/episodes`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/episodes', H['create_episode'], methods=['POST'])
    router.add_api_route('/episodes/{memory_id}', H['get_episode'], methods=['GET'])
