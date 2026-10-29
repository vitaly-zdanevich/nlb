'''Exercise conservative publication evidence and Commons license selection offline.'''

import unittest

from publication_rights import evaluate_publication, publication_year


DEFAULT_LICENSE = 'PD-scan|PD-old-assumed-expired'


def contributor(name: str, dates: list[str], roles: list[str] | None = None) -> dict:
	'''Construct a personal contributor with explicit UNIMARC date evidence.'''
	return {'name': name, 'dates': dates, 'roles': roles or ['070'], 'tag': '700', 'kind': 'person'}


def coded_date(value: str) -> dict:
	'''Return lossless MARC 100 data with its date-type character preserved.'''
	return {'raw_marc_fields': [{'kind': 'datafield', 'tag': '100', 'subfields': [{'code': 'a', 'value': '20200101' + value}]}]}


class TestPublicationRights(unittest.TestCase):
	'''Use a fixed current year so copyright boundaries do not drift with the clock.'''

	def evaluate(self, marc: dict, record: dict | None = None, **kwargs) -> dict:
		'''Apply the default policy against deterministic 2026 thresholds.'''
		return evaluate_publication(record or {}, marc, DEFAULT_LICENSE, current_year=2026, **kwargs)

	def test_title_numbers_never_become_publication_dates(self) -> None:
		'''The subject year 1812 supplies neither publication evidence nor a license.'''
		record = {'title': 'История войны 1812 года [[Изоматериал]]'}
		self.assertIsNone(publication_year(record, {}))
		decision = self.evaluate({}, record)
		self.assertFalse(decision['eligible'])
		self.assertEqual(decision['basis'], 'rights_review')

	def test_assumed_age_requires_strictly_more_than_120_years(self) -> None:
		'''At the 120-year boundary, the whole publication year must have elapsed.'''
		for year, eligible in ((1890, True), (1905, True), (1906, False), (1917, False)):
			with self.subTest(year=year):
				decision = self.evaluate({'date': str(year)})
				self.assertEqual(decision['eligible'], eligible)
				self.assertEqual((decision['lower_year'], decision['upper_year']), (year, year))
				if eligible:
					self.assertEqual(decision['license'], DEFAULT_LICENSE)

	def test_uncertain_open_and_reprint_dates_require_review(self) -> None:
		'''Probable dates, open intervals and reprints are not exact old publications.'''
		for date in ('[1890?]', 'около 1890', 'ca. 1890', 'не ранее 1890', 'после 1890', '1890-', '1890, перепеч. 2010', '1890; reprint 2010', '[б. г.]'):
			with self.subTest(date=date):
				self.assertFalse(self.evaluate({'date': date})['eligible'])

	def test_bounded_dates_use_latest_possible_year(self) -> None:
		'''Ranges and explicit upper bounds are useful while retaining their uncertainty.'''
		for date, lower, upper, eligible in (
			('[1890]', 1890, 1890, True),
			('1890—1905', 1890, 1905, True),
			('1890—1910', 1890, 1910, False),
			('between 1890 and 1900', 1890, 1900, True),
			('не позднее 1890', None, 1890, True),
			('до 1890', None, 1889, True),
		):
			with self.subTest(date=date):
				decision = self.evaluate({'date': date})
				self.assertEqual((decision['lower_year'], decision['upper_year']), (lower, upper))
				self.assertEqual(decision['eligible'], eligible)
				self.assertEqual(publication_year({}, {'date': date}), lower)

	def test_conflicting_date_sources_are_not_silently_overridden(self) -> None:
		'''HTML and MARC disagreement remains visible and prevents automatic upload.'''
		record = {'fields': [{'name': 'Опубликовано', 'value': 'Минск : Издатель, 2010'}]}
		decision = self.evaluate({'date': '1890'}, record)
		self.assertFalse(decision['eligible'])
		self.assertIn('conflict', decision['reason'])
		self.assertIsNone(publication_year(record, {'date': '1890'}))
		self.assertEqual(len(decision['evidence']), 2)

	def test_coded_types_retain_range_unknown_and_reprint_semantics(self) -> None:
		'''The first MARC 100 year alone cannot certify ranges or reproductions.'''
		for code, eligible, lower, upper in (
			('d1890    ', True, 1890, 1890),
			('g18901910', False, 1890, 1910),
			('f18801890', True, 1880, 1890),
			('c1890    ', False, 1890, None),
			('a18909999', False, 1890, None),
			('e20101890', False, 2010, 2010),
			('u        ', False, None, None),
		):
			with self.subTest(code=code):
				decision = self.evaluate(coded_date(code))
				self.assertEqual(decision['eligible'], eligible)
				self.assertEqual((decision['lower_year'], decision['upper_year']), (lower, upper))

	def test_uncertain_display_date_is_not_promoted_by_coded_exact_year(self) -> None:
		'''MARC 100 code d may still accompany an explicitly uncertain 210 date.'''
		meta = coded_date('d1890    ')
		meta.update(date='[1890?]', year='1890')
		self.assertFalse(self.evaluate(meta)['eligible'])

	def test_summary_year_does_not_shorten_a_publication_range(self) -> None:
		'''A coded beginning year cannot erase later volumes in the publication statement.'''
		meta = coded_date('d1890    ')
		meta['date'] = '1890—1910'
		decision = self.evaluate(meta)
		self.assertFalse(decision['eligible'])
		self.assertEqual((decision['lower_year'], decision['upper_year']), (1890, 1910))

	def test_qualified_bounds_and_reversed_lifespans_need_review(self) -> None:
		'''Uncertain limits and impossible birth/death order do not establish expiry.'''
		for date in ('before 1890?', 'до 1890?', 'not later than ca. 1890', 'не ранее 1890, не позднее 1910'):
			with self.subTest(date=date):
				self.assertFalse(self.evaluate({'date': date})['eligible'])
		self.assertFalse(self.evaluate({'date': '1890', 'contributors': [contributor('Author', ['1800-1700'])]})['eligible'])

	def test_known_creators_use_latest_death_and_conservative_100_year_rule(self) -> None:
		'''All creative contributors must have resolved dates before switching licenses.'''
		meta = {'date': '1890', 'contributors': [contributor('Author', ['1800-1870']), contributor('Translator', ['1820–1900'], ['730'])]}
		decision = self.evaluate(meta)
		self.assertTrue(decision['eligible'])
		self.assertEqual(decision['license'], 'PD-scan|PD-old-auto-expired|deathyear=1900')
		self.assertEqual(decision['basis'], 'known_death_dates')
		for death, eligible in ((1925, True), (1926, False), (1970, False)):
			with self.subTest(death=death):
				meta['contributors'] = [contributor('Author', [f'1850-{death}'])]
				self.assertEqual(self.evaluate(meta)['eligible'], eligible)

	def test_partially_known_or_uncertain_creator_dates_require_review(self) -> None:
		'''Known death evidence cannot be discarded to fall back to the assumed template.'''
		for dates in ([], ['1840-'], ['1840-1900?'], ['1840-1900', '1840-1910']):
			with self.subTest(dates=dates):
				meta = {'date': '1890', 'contributors': [contributor('Author', ['1800-1870']), contributor('Translator', dates, ['730'])]}
				self.assertFalse(self.evaluate(meta)['eligible'])

	def test_noncreative_roles_do_not_supply_creator_death_dates(self) -> None:
		'''Former owners, printers and editors are not silently treated as authors.'''
		meta = {'date': '1890', 'contributors': [contributor('Author', []), contributor('Owner', ['1900-1980'], ['390']), contributor('Editor', ['1850-1950'], ['340'])]}
		decision = self.evaluate(meta)
		self.assertTrue(decision['eligible'])
		self.assertEqual(decision['license'], DEFAULT_LICENSE)

	def test_organization_lifespan_is_not_a_personal_death_date(self) -> None:
		'''Institution operating years cannot select a personal death-year template.'''
		meta = {'date': '1890', 'contributors': [{'name': 'Academy', 'dates': ['1800-1900'], 'kind': 'organization', 'tag': '710', 'roles': ['070']}]}
		decision = self.evaluate(meta)
		self.assertFalse(decision['eligible'])

	def test_custom_license_is_recorded_user_attestation(self) -> None:
		'''Explicit custom licenses can authorize records outside automatic assumptions.'''
		decision = evaluate_publication({}, {}, 'CC-BY-SA-4.0', current_year=2026)
		self.assertTrue(decision['eligible'])
		self.assertEqual(decision['basis'], 'user_attestation')
		self.assertEqual(decision['license'], 'CC-BY-SA-4.0')
		self.assertTrue(self.evaluate({}, explicit_license=True)['eligible'])

	def test_recent_publication_cannot_use_us_expired_with_old_author(self) -> None:
		'''Known ancient authors do not establish expiry for a recent publication.'''
		meta = {'date': '2000', 'contributors': [contributor('Author', ['1700-1780'])]}
		decision = self.evaluate(meta)
		self.assertFalse(decision['eligible'])
		self.assertIn('United States', decision['reason'])

	def test_lossless_repeated_dates_and_legacy_evidence_are_retained(self) -> None:
		'''Both snapshot formats supply all dates instead of only the parser's first.'''
		meta = {'raw_marc_fields': [{'tag': '210', 'subfields': [{'code': 'd', 'value': '1890'}, {'code': 'd', 'value': '2010'}]}]}
		self.assertFalse(self.evaluate(meta)['eligible'])
		self.assertEqual(len(self.evaluate(meta)['evidence']), 2)
		legacy = {'raw_fields': [{'tag': '100', 'value': '$a 20200101d1890    '}]}
		self.assertTrue(self.evaluate(legacy)['eligible'])
		self.assertEqual(publication_year({}, {'year': '1880'}), 1880)
		record = {'fields': [{'name': 'Автор', 'values': ['Author (1800-1870)']}, {'name': 'Опубликовано', 'value': '1890'}]}
		self.assertEqual(self.evaluate({}, record)['license'], 'PD-scan|PD-old-auto-expired|deathyear=1870')

	def test_214_only_publication_activity_supplies_publication_dates(self) -> None:
		'''Production, distribution and copyright years cannot stand in for publication.'''
		for indicator in ('0', '1', '2', '3', '4'):
			with self.subTest(indicator=indicator):
				meta = {'raw_marc_fields': [{'tag': '214', 'ind2': indicator, 'subfields': [{'code': 'd', 'value': '1890'}]}]}
				self.assertEqual(self.evaluate(meta)['eligible'], indicator == '1')

	def test_unresolved_roles_and_imprecise_lifespans_are_reviewed(self) -> None:
		'''Unknown roles and nonauthor name dates cannot silently establish expiry.'''
		for roles in (['000'], ['570'], ['330']):
			with self.subTest(roles=roles):
				self.assertFalse(self.evaluate({'date': '1890', 'contributors': [contributor('Person', [], roles)]})['eligible'])
		meta = {'date': '1890', 'contributors': [{'name': 'Unspecified secondary (1900-1980)', 'tag': '702', 'roles': []}]}
		self.assertTrue(self.evaluate(meta)['eligible'])
		meta['contributors'] = [{'name': 'Primary (1800-1870)', 'tag': '700', 'roles': []}]
		self.assertIn('deathyear=1870', self.evaluate(meta)['license'])

	def test_malformed_and_abbreviated_dates_remain_unresolved(self) -> None:
		'''Unsupported numeric patterns must not degrade to the first plausible year.'''
		for date in ('1900–1890', '1890, 1900', '1890–95'):
			with self.subTest(date=date):
				self.assertFalse(self.evaluate({'date': date})['eligible'])
		for code in ('d18', 'g18909999', 'z1890    '):
			with self.subTest(code=code):
				self.assertFalse(self.evaluate(coded_date(code))['eligible'])
		meta = {'date': '1890'}
		record = {'fields': [{'name': 'Опубликовано', 'value': '1890'}, {'name': 'Опубликовано', 'value': '1890'}]}
		self.assertTrue(self.evaluate(meta, record)['eligible'])


if __name__ == '__main__':
	unittest.main()
