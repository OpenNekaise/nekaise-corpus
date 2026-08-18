import find_govuk


def test_title_relevance_keeps_explicit_built_environment_titles():
    titles = [
        "How to assess and allocate sites for development",
        "Inclusive mobility: guidance on the use of tactile paving surfaces",
        "MA04 Land quality baseline data",
        "Flood risk: Upper Aire management strategy",
        "Energy performance certificates for non-domestic buildings",
        "Approved Document L: conservation of fuel and power",
        "Future Homes Standard: 2025 consultation response",
        "Standard Assessment Procedure for energy rating of dwellings",
        "Digest of UK Energy Statistics 2025",
        "Energy Company Obligation: ECO4 guidance",
    ]

    assert all(find_govuk.title_relevant(title) for title in titles)


def test_title_relevance_rejects_deep_search_false_positives():
    titles = [
        "Armed forces continuous working patterns survey 2017/18",
        "Crab and lobster stock assessment 2017",
        "Food Information Regulations 2013",
        "PIP breast implants: interim report",
        "The nature of online offending",
        "Child Support Fees Regulations 2012: equality impact assessment",
        "Growing the automotive supply chain: the road forward",
    ]

    assert not any(find_govuk.title_relevant(title) for title in titles)
