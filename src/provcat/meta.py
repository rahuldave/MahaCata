
def synthesize_name_and_description(uri:str, yaml_cfg_path:str=None):
    # simple synthesis fallback
    name = uri.split('://',1)[1].split('?')[0].strip('/').replace('/',' • ')
    desc = f'Auto-generated card for {uri}'
    return name, desc
