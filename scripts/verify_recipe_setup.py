"""Verify recipe onboarding, batch estimates and registration form fields.

Requires Playwright and a browser. All data is confined to SQLite in memory.
Run: python scripts/verify_recipe_setup.py
"""
import os,sys,threading,json,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
os.environ.update(DATABASE_URL='sqlite:///:memory:',STRIPE_DISABLED='true',SECRET_KEY='qa-setup',PATIA_AI_ENABLED='false')
from app import create_app,db
from app.models import User,Product,Recipe,Sale
from app.team.services import ensure_owner_organization
from werkzeug.serving import make_server
from playwright.sync_api import sync_playwright
app=create_app();app.config.update(TESTING=True,WTF_CSRF_ENABLED=False,RATELIMIT_ENABLED=False,SESSION_COOKIE_SECURE=False)
with app.app_context():
    db.create_all();owner=User(email='recipe-browser@test.local',company_name='QA',email_verified=True,trial_plan_code='restaurant');owner.set_password('Password123');db.session.add(owner);db.session.flush();member=ensure_owner_organization(owner);member.organization.business_type='restaurant';db.session.commit();uid=owner.id
client=app.test_client()
with client.session_transaction() as session:session['user_id']=uid
cookie=client.get_cookie('session').value
server=make_server('127.0.0.1',0,app,threaded=True);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
try:
    with sync_playwright() as pw:
        edge=Path(r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe')
        options={'headless':True}
        if edge.exists(): options['executable_path']=str(edge)
        browser=pw.chromium.launch(**options)
        for width in (1440,390):
            context=browser.new_context(viewport={'width':width,'height':900});page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            page.route('**/*',lambda route:route.continue_() if route.request.url.startswith(base) else route.abort())
            page.goto(base+'/register',wait_until='networkidle');page.locator('#country-code').select_option('US')
            data=page.evaluate("Object.fromEntries(new FormData(document.getElementById('auth-form')))")
            assert data['country_code']=='US' and data['currency_code']=='USD' and data['locale_code']=='en_US',data
            context.add_cookies([{'name':'session','value':cookie,'url':base}])
            page.goto(base+'/recipes/new',wait_until='networkidle')
            if width==1440:
                assert page.locator('#add-component').is_disabled()
                assert page.locator('#recipe-submit').is_disabled()
                assert page.get_by_role('link',name='Cargar o revisar ingredientes').is_visible()
            page.goto(base+'/products',wait_until='networkidle')
            content=f'SKU,Nombre del producto,Costo,Precio de venta,Stock inicial,Unidad base\nHAR-{width},Harina {width},20,0,10,kg\n'.encode()
            page.locator('#catalog-file').set_input_files({'name':'ingredientes.csv','mimeType':'text/csv','buffer':content})
            with page.expect_response('**/api/products/import/preview'):page.locator('#catalog-import-submit').click()
            with page.expect_response('**/api/products/import/commit'):page.locator('#catalog-import-confirm').click()
            page.locator('#catalog-import-recipes').click();page.wait_for_url('**/recipes/new')
            assert not page.locator('#recipe-submit').is_enabled()
            page.locator('#add-component').click()
            with app.app_context():pid=Product.query.filter_by(sku=f'HAR-{width}').one().id
            page.locator('#component-list select').first.select_option(f'product:{pid}')
            page.locator('#component-list select').last.select_option('g')
            page.locator('#component-list input').fill('1000');page.locator('#recipe-price').fill('30');page.locator('#yield-quantity').fill('10')
            assert '2.00' in page.locator('#estimated-cost').inner_text()
            assert '28.00' in page.locator('#estimated-profit').inner_text()
            assert page.locator('#estimated-margin').inner_text()=='93.3%'
            page.locator('#yield-quantity').fill('5');assert '4.00' in page.locator('#estimated-cost').inner_text()
            page.locator('#recipe-type').select_option('preparation');assert '20.00' in page.locator('#estimated-cost').inner_text();assert page.locator('#estimated-profit').inner_text()=='\u2014'
            page.locator('#recipe-type').select_option('dish');page.locator('#yield-quantity').fill('');assert page.locator('#recipe-submit').is_disabled();assert page.locator('#estimated-cost').inner_text()=='\u2014'
            page.locator('#yield-quantity').fill('10');page.locator('#yield-unit').select_option('piece');page.locator('#recipe-name').fill(f'Roles QA {width}')
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
            page.locator('#recipe-submit').click();page.wait_for_url('**/recipes/*')
            assert '/recipes/new' not in page.url
            with app.app_context():
                recipe=Recipe.query.filter_by(name=f'Roles QA {width}').one();assert recipe.sale_product.cost_price==2;rid=recipe.sale_product_id
            sale=client.post('/sell-cart',json={'request_id':str(uuid.uuid4()),'payment_method':'card','items':[{'product_id':rid,'quantity':2}]})
            assert sale.status_code==200,sale.get_json()
            with app.app_context():
                assert str(Product.query.filter_by(id=pid).one().stock)=='9.800'
            assert not errors,errors
            print(f'PASS {width}px registration fields, inventory-to-recipe, empty state, batch costs, save and ingredient sale',flush=True)
            context.close()
        browser.close()
finally:
    server.shutdown();thread.join(timeout=5)
    with app.app_context():db.session.remove();db.engine.dispose()
