from analyzers.accessibility import AccessibilityAnalyzer
from analyzers.links import LinkAnalyzer

ANALYZERS = {analyzer.name: analyzer for analyzer in (LinkAnalyzer, AccessibilityAnalyzer)}
