import unittest
from app import app

class LayoutRenderingTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_dashboard_contains_category_card_and_matrix_cards(self):
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="tags"', html)
        self.assertIn('luxury-card rounded-2xl', html)
        self.assertIn('grid grid-cols-1 lg:grid-cols-3', html)

    def test_category_card_responsive_heights_present(self):
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)
        self.assertIn('h-48 md:h-56 lg:h-64', html)

    def test_primary_stats_have_hover_targets(self):
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)
        self.assertIn('class="js-hover-hours"', html)

if __name__ == "__main__":
    unittest.main()
