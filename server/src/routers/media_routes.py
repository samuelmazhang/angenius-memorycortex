"""Route declarations for the ``/media`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/media', H['create_media'], methods=['POST'])
    router.add_api_route('/media/search', H['search_media'], methods=['GET'])
    router.add_api_route('/media/by-type/{media_type}', H['media_by_type'], methods=['GET'])
