"""Real owner setup, preference changes, role menus and device screenshots."""
import importlib.util
import os
from pathlib import Path
from threading import Thread
import unittest

from werkzeug.serving import make_server, WSGIRequestHandler
from app import db, limiter
from app.work_style import initialize_new_owner
from test_cashier_session import CashierFixture


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'Playwright is optional')
class OwnerWorkStyleBrowserTests(unittest.TestCase):
    def test_owner_choices_and_employee_menus_desktop_mobile(self):
        from playwright.sync_api import sync_playwright
        audit=CashierFixture()
        self.addCleanup(audit.close)
        # Several role logins from one disposable browser server must not consume
        # production-style per-IP login limits. Restore the limiter after this test.
        previous_enabled=limiter.enabled
        limiter.enabled=False
        self.addCleanup(setattr,limiter,'enabled',previous_enabled)
        manager,_=audit.fixture.add_member('MANAGER','manager.demo@example.com')
        initialize_new_owner(audit.owner)
        db.session.commit()
        audit.app.config['WTF_CSRF_ENABLED']=True

        class QuietHandler(WSGIRequestHandler):
            def log(self,*args,**kwargs):
                pass
        server=make_server('127.0.0.1',0,audit.app,request_handler=QuietHandler)
        thread=Thread(target=server.serve_forever,daemon=True)
        thread.start()
        def stop():
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
        self.addCleanup(stop)
        origin=f'http://127.0.0.1:{server.server_port}'
        folder=Path(os.environ['PATIA_WORK_STYLE_ARTIFACTS']) if os.environ.get('PATIA_WORK_STYLE_ARTIFACTS') else None
        if folder:
            folder.mkdir(parents=True,exist_ok=True)

        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(channel='msedge')
            try:
                def logged_context(email,password):
                    context=browser.new_context(viewport={'width':1440,'height':1000})
                    context.route('**/*',lambda route: route.continue_() if route.request.url.startswith(origin) else route.abort())
                    page=context.new_page()
                    page.goto(origin+'/login',wait_until='domcontentloaded')
                    page.locator('input[name="email"]').fill(email)
                    page.locator('input[name="password"]').fill(password)
                    with page.expect_navigation(wait_until='domcontentloaded'):
                        page.locator('#auth-form button[type="submit"]').click()
                    self.assertEqual(page.locator('#primary-navigation').count(),1, page.locator('body').inner_text()[:1000])
                    return context,page

                def capture(page,name):
                    for width in (1440,390):
                        page.set_viewport_size({'width':width,'height':1000})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth <= innerWidth'))
                        toggle=page.locator('.sidebar-v2__toggle')
                        if width==390 and toggle.get_attribute('aria-expanded')!='true':
                            toggle.click()
                        self.assertTrue(page.locator('#primary-navigation').is_visible())
                        self.assertIn('Cerrar sesión',page.locator('#primary-navigation').inner_text())
                        self.assertTrue(page.get_by_text('Ayuda',exact=True).first.is_visible())
                        self.assertEqual(page.locator('#primary-navigation a[href="/admin"]').count(),0)
                        if folder:
                            page.screenshot(path=str(folder/f'{name}-{width}.png'),full_page=True)

                context,page=logged_context(audit.owner.email,'Password123')
                page.goto(origin+'/',wait_until='domcontentloaded')
                form=page.locator('[data-work-style-form]')
                self.assertEqual(form.count(),1)
                self.assertEqual(page.locator('#onboarding-title').count(),1)
                form.locator('input[value="team"]').check()
                self.assertTrue(form.locator('[data-owner-activity]').is_visible())
                self.assertTrue(form.get_by_text('Cada persona con su propio acceso',exact=True).is_visible())
                form.locator('input[value="supervision"]').check()
                capture(page,'asistente-equipo')
                with page.expect_navigation(wait_until='domcontentloaded'):
                    form.locator('button[value="defer"]').click()
                self.assertEqual(page.locator('[data-work-style-form]').count(),0)
                capture(page,'menu-actual')

                for mode,style,activity,expected in [
                    ('solo','solo',None,['home','pos','inventory','cash']),
                    ('equipo-cobra','team','operations',['home','pos','inventory','cash']),
                    ('equipo-supervisa','team','supervision',['home','reports','decisions'])]:
                    page.goto(origin+'/settings#work-style',wait_until='domcontentloaded')
                    form=page.locator('[data-work-style-form]')
                    form.locator(f'input[name="work_style"][value="{style}"]').check()
                    if activity:
                        form.locator(f'input[name="owner_activity"][value="{activity}"]').check()
                    else:
                        self.assertFalse(form.locator('[data-owner-activity]').is_visible())
                    with page.expect_navigation(wait_until='domcontentloaded'):
                        form.locator('button[value="save"]').click()
                    capture(page,f'configuracion-{mode}')
                    page.goto(origin+'/',wait_until='domcontentloaded')
                    keys=page.locator('#primary-navigation [data-work-nav]').evaluate_all('(links) => links.map(a=>a.dataset.workNav)')
                    self.assertEqual(keys[:len(expected)],expected)
                    self.assertEqual(len(keys),len(set(keys)))
                    self.assertEqual(page.locator('[data-work-style-form]').count(),0)
                    capture(page,mode)
                # Server persistence across logout and a fresh browser profile.
                with page.expect_navigation(wait_until='domcontentloaded'):
                    page.locator('.sidebar-v2__logout-button').click()
                context.close()
                context,page=logged_context(audit.owner.email,'Password123')
                page.goto(origin+'/',wait_until='domcontentloaded')
                self.assertEqual(page.locator('#primary-navigation [data-work-nav]').evaluate_all('(links)=>links.slice(0,3).map(a=>a.dataset.workNav)'),['home','reports','decisions'])
                self.assertEqual(page.locator('[data-work-style-form]').count(),0)
                context.close()

                for name,email,password,home in [('encargado',manager.email,'Password123','/'),('cajero',audit.email,audit.password,'/sell')]:
                    employee_context,employee=logged_context(email,password)
                    employee.goto(origin+'/',wait_until='domcontentloaded')
                    if folder and employee.url != origin+home:
                        employee.screenshot(path=str(folder/f'{name}-unexpected.png'),full_page=True)
                    self.assertEqual(employee.url,origin+home)
                    self.assertEqual(employee.locator('[data-work-style-form]').count(),0)
                    self.assertEqual(employee.locator('[data-work-nav]').count(),0)
                    if name=='cajero':
                        for href in ('/reports','/products','/settings','/team','/subscription'):
                            self.assertEqual(employee.locator(f'#primary-navigation a[href="{href}"]').count(),0)
                    else:
                        self.assertEqual(employee.locator('#primary-navigation a[href="/reports"]').count(),1)
                    capture(employee,name)
                    employee_context.close()
            finally:
                browser.close()
