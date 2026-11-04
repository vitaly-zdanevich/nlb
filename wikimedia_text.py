'''Normalize generated Commons text without changing archived source metadata.

MediaWiki uses Unicode NFC and rejects XML-illegal C0 controls. Keep ordinary
text whitespace and compatibility characters, so this is not a content rewrite.
https://www.mediawiki.org/wiki/Unicode_normalization_considerations
https://www.mediawiki.org/wiki/Utfnormal
'''

import unicodedata


_INVALID_CONTROLS = {code: None for code in range(32) if code not in (9, 10, 13)}


def normalize_wikitext(text: str) -> str:
	'''Compose canonically equivalent characters and remove forbidden C0 controls.

	Use only on generated output or comparison copies. Original source bytes,
	parsed metadata, archive checksums and exact edit receipts must stay unchanged.
	'''
	return unicodedata.normalize('NFC', text.translate(_INVALID_CONTROLS))
