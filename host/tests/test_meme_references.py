import sys,base64,json
from pathlib import Path
from types import SimpleNamespace
from PIL import Image
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from astrbot_whale_device.memes.references import fixed_reference,TOSReferenceUploader,ReferenceUploadNotConfigured
from astrbot_whale_device.jimeng_client import JimengClient,Credentials

def test_dedicated_and_quarter_character_references(tmp_path):
 root=tmp_path
 images=root/'images';images.mkdir()
 for name in ['Gpt母图【Q版】.png','Claude母图【Q版】.png','deepseek母图【非Q版】.jpg','AI设定母图【gpt、ds【Q版】、GLM、claude】.webp']:
  Image.new('RGB',(384,512),'blue').save(images/name)
 assert fixed_reference(root,'gpt_q').path.name=='Gpt母图【Q版】.png'
 assert fixed_reference(root,'claude_q').path.name=='Claude母图【Q版】.png'
 ref=fixed_reference(root,'ds_q');assert ref.crop==(200,281,372,458)
 assert ref.bytes().startswith(b'\x89PNG')
 assert fixed_reference(root,'deepseek').path.name=='deepseek母图【非Q版】.jpg'

def test_private_upload_signs_and_actual_generation_request_carries_image(tmp_path):
 source=tmp_path/'images';source.mkdir();Image.new('RGB',(384,512),'blue').save(source/'AI设定母图【gpt、ds【Q版】、GLM、claude】.webp')
 ref=fixed_reference(tmp_path,'ds_q');calls=[]
 class Fake:
  def put_object(self,bucket,key,**kwargs):
   assert kwargs['content'].startswith(b'\x89PNG');assert kwargs['content_type']=='image/png';calls.append('put')
  def pre_signed_url(self,method,**kwargs):
   assert kwargs['expires']<=900;calls.append('sign');return SimpleNamespace(signed_url='https://example.com/private-reference?signature=fake')
 uploader=TOSReferenceUploader(ak='test',sk='test',bucket='private-test',region='cn-beijing',endpoint='https://tos-cn-beijing.volces.com',client_factory=lambda *a:Fake())
 url=uploader.upload(ref);requests=[]
 def transport(url,headers,body,timeout):requests.append(json.loads(body));return b'{"code":10000,"data":{"task_id":"fake"}}'
 client=JimengClient(Credentials('test','test'),transport=transport);client.submit('use reference',verified_fields={'image_urls':[url],'size':1048576})
 assert calls==['put','sign'];assert requests[0]['image_urls']==[url];assert requests[0]['force_single'] is True
