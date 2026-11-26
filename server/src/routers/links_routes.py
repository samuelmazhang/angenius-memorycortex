"""Route declarations for the ``/links`` domain (P0 phase 3).

Handlers are passed in via ``H`` (a dict of api_pg functions) so this
module never imports api_pg -- no circular import.
"""
from fastapi import APIRouter

def build(router, H):
    router.add_api_route('/links/create', H['link_create'], methods=['POST'])
    router.add_api_route('/links/{source_id}', H['get_links'], methods=['GET'])
    router.add_api_route('/links/access', H['link_access'], methods=['POST'])
    router.add_api_route('/links/path/{source_id}/{target_id}', H['link_path'], methods=['GET'])
    router.add_api_route('/links/decay', H['link_decay'], methods=['POST'])
