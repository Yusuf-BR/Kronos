# Reconciler Module Quick Reference

## Quick Start

### Basic Usage

```python
from agents.reconciler import ReconcilerAgent

# Initialize the reconciler
reconciler = ReconcilerAgent()

# Prepare extracted data (typically from ExtractorAgent)
extracted_data = {
    "filename": "document.pdf",
    "entities": [
        {
            "name": "John Doe",
            "type": "Person",
            "description": "Researcher at MIT",
            "claims": [
                {"text": "Has 42 published papers"},
                {"text": "Specializes in artificial intelligence"}
            ]
        }
    ]
}

# Run reconciliation
result = reconciler.reconcile(extracted_data)

# Close the reconciler when done
reconciler.close()
```

## Configuration

### Environment Variables

```python
# In your config file (core/config.py)
RECONCILER_BACKEND = "groq"  # or "mistral"
RECONCILER_MODEL = "llama3-70b-8192"  # Groq model
RECONCILER_MISTRAL_MODEL = "mistral-large-latest"  # Mistral model
```

### API Keys

```python
# Required API keys (in your environment or config)
GROQ_API_KEY = "your-groq-api-key"
MISTRAL_API_KEY = "your-mistral-api-key"
```

## Input Data Format

The ReconcilerAgent expects data in this format:

```python
extracted_data = {
    "filename": "document.pdf",  # Source document name
    "entities": [  # List of entities to reconcile
        {
            "name": "Entity Name",  # Entity name
            "type": "Entity Type",  # Entity type (Person, Organization, etc.)
            "description": "Entity description",  # Optional description
            "claims": [  # Optional list of claims about the entity
                {"text": "Claim text"},
                {"text": "Another claim"}
            ]
        }
    ]
}
```

## Output Format

The ReconcilerAgent returns reconciliation statistics:

```python
result = {
    "filename": "document.pdf",  # Processed document
    "conflicts_found": 5,  # Number of conflicts detected
    "conflicts_resolved": 3,  # Number of conflicts resolved
    "conflicts_flagged": 2,  # Number of conflicts flagged for review
    "unverified_count": 1,  # Number of unverified conflicts
    "details": [  # Detailed conflict information
        {
            "entity_name": "Entity Name",
            "entity_type": "Entity Type",
            "new_doc": "document.pdf",
            "new_description": "New description",
            "existing_doc": "existing.pdf",
            "existing_description": "Existing description",
            "resolution": {
                "is_contradiction": True,
                "conflict_type": "CONTRADICTION",
                "contradicting_attribute": "papers_count",
                "confidence": 0.95,
                "resolution": "KEEP_DOC2",
                "reason": "Specific explanation of the conflict"
            }
        }
    ]
}
```

## Common Resolution Outcomes

| Resolution | Meaning | Action Taken |
|------------|---------|--------------|
| KEEP_DOC1 | Existing document is more credible | Keep existing entity, reduce confidence |
| KEEP_DOC2 | New document is more credible | Update entity with new information |
| KEEP_BOTH | Both documents are valid | Create complementary relationship |
| FLAG_FOR_REVIEW | Conflict requires human judgment | Create conflict edge, flag entity |
| MERGE | Combine both descriptions | Create merged entity |

## Conflict Types

| Type | Meaning | Example |
|------|---------|---------|
| CONTRADICTION | Directly incompatible values | "42 papers" vs "31 papers" |
| UPDATE | Newer document supersedes older | "Founded in 2019" vs "Founded in 2021" |
| COMPLEMENTARY | Different aspects of same entity | "Researcher" vs "Teacher" |
| DUPLICATE | Same information expressed differently | "Has 42 papers" vs "Published 42 papers" |

## Troubleshooting

### Common Issues

1. **High Conflict Rate**:
   - Check source reliability scores
   - Review extraction quality
   - Consider adjusting confidence thresholds

2. **Failed LLM Calls**:
   - Check API quota and limits
   - Verify API keys are correct
   - Review error logs for specific messages

3. **Performance Issues**:
   - Check database query performance
   - Review claim encoding efficiency
   - Consider batching reconciliation calls

### Debugging Tips

1. **Enable Detailed Logging**:
   ```python
   import logging
   logging.basicConfig(level=logging.INFO)
   ```

2. **Check Neo4j Relationships**:
   ```cypher
   MATCH (e:Entity)-[r:CONFLICTS_WITH]->(other:Entity)
   RETURN e, r, other
   LIMIT 100
   ```

3. **Review Source Reliability**:
   ```python
   from agents.source_reliability import SourceReliability
   sr = SourceReliability()
   print(sr.get_reliability_scores())
   ```

## Best Practices

1. **Regular Reconciliation**:
   - Run reconciliation periodically to keep the knowledge graph consistent
   - Consider scheduling reconciliation during off-peak hours

2. **Source Credibility Tracking**:
   - Monitor source reliability scores regularly
   - Adjust credibility thresholds as needed
   - Consider manual overrides for known reliable/unreliable sources

3. **Error Handling**:
   - Implement retry logic for failed LLM calls
   - Log detailed error information for troubleshooting
   - Consider fallback mechanisms for critical failures

4. **Performance Optimization**:
   - Batch reconciliation calls when possible
   - Consider parallel processing for large documents
   - Monitor and optimize database queries

## Performance Tips

1. **Claim Encoding**:
   - Claims are encoded once upfront for efficiency
   - Large documents with many claims may take longer to process

2. **Vector Similarity Search**:
   - Uses cosine similarity for efficient claim ranking
   - Top-k results are used to limit processing

3. **API Usage**:
   - Each conflict resolution requires an LLM call
   - Monitor API usage to avoid exceeding quotas

## Integration Checklist

- [ ] Configure API keys for Groq/Mistral
- [ ] Set up Neo4j and Qdrant connections
- [ ] Configure reconciliation parameters
- [ ] Set up logging and monitoring
- [ ] Implement error handling and retries
- [ ] Test with sample documents
- [ ] Review source reliability scores
- [ ] Schedule regular reconciliation runs

## References

- [Reconciler Module Documentation](reconciler_module.md)
- [Agent Framework Documentation](agent_framework.md)
- [Data Processing Module Documentation](data_processing.md)
- [Utilities Module Documentation](utilities.md)
- [Neo4j Documentation](https://neo4j.com/docs/)
- [Qdrant Documentation](https://qdrant.tech/documentation/)
- [Groq API Documentation](https://console.groq.com/docs)
- [Mistral AI Documentation](https://docs.mistral.ai/)