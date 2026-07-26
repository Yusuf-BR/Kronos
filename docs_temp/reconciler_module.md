# Reconciler Module Documentation

## Overview

The Reconciler Module is a critical component of the KRONOS knowledge management system that resolves conflicts between different sources of information about the same entities. It ensures data consistency by detecting contradictions, complementary information, or updates across documents.

The module is part of the `agent_framework` and specifically handles the reconciliation of entity information extracted from various documents. It operates as part of the memory systems pipeline, working with entities extracted by other agents like the ExtractorAgent and CuratorAgent.

## Purpose and Core Functionality

The Reconciler Module's primary purpose is to:

1. **Detect Conflicts**: Identify when different documents describe the same entity with contradictory information
2. **Resolve Conflicts**: Determine which source is more credible based on various factors
3. **Maintain Knowledge Integrity**: Ensure the knowledge graph remains consistent and reliable
4. **Handle Updates**: Recognize when newer information supersedes older information
5. **Flag Ambiguities**: Identify cases that require human review

### Key Features

- **Cross-Domain Reconciliation**: Works across any domain (people, organizations, products, financial figures, etc.)
- **Attribute-Level Analysis**: Focuses on specific attributes rather than just high-level descriptions
- **Context-Aware Resolution**: Considers the context and credibility of sources
- **Efficient Processing**: Uses pre-encoded claim vectors to avoid redundant processing
- **Multiple Backend Support**: Can use either Groq or Mistral AI as the LLM backend

## Architecture and Component Relationships

```mermaid
diagram TD
    A[ReconcilerAgent] --> B[Neo4jClient]
    A --> C[SourceReliability]
    A --> D[KronosQdrantClient]
    A --> E[Groq/Mistral API]
    
    F[ExtractorAgent] -->|Extracts entities| A
    G[CuratorAgent] -->|Curates entities| A
    H[CodexAgent] -->|Processes documents| A
    
    I[Config] --> A
    J[QuotaTracker] --> A
    
    K[Neo4j Database] --> B
    L[Qdrant Vector DB] --> D
    
    style A fill:#f9f,stroke:#333
```

### Component Dependencies

1. **Neo4jClient**: For storing and querying entity relationships and conflict edges
2. **SourceReliability**: For tracking the credibility of different sources based on resolution outcomes
3. **KronosQdrantClient**: For vector similarity search of claims
4. **Groq/Mistral API**: For natural language processing and conflict resolution decisions
5. **Config**: For configuration settings like backend selection and model parameters
6. **QuotaTracker**: For managing API usage quotas

## Data Flow and Process

```mermaid
diagram TD
    A[New Document] --> B[ExtractorAgent]
    B --> C[Extract Entities and Claims]
    C --> D[ReconcilerAgent]
    
    D --> E[Check for Existing Entities in Neo4j]
    E -->|Found| F[Compare with Existing Entities]
    E -->|Not Found| G[Skip Reconciliation]
    
    F --> H[Detect Conflicts]
    H -->|Conflict Found| I[Resolve Conflict]
    H -->|No Conflict| J[Mark as Complementary]
    
    I --> K[Apply Resolution]
    K --> L[Update Neo4j with Resolution]
    K --> M[Update Source Reliability Scores]
    
    J --> N[Create Complementary Edge]
    
    I --> O[Flag for Review if Needed]
    O --> P[Create Conflict Edge]
    
    style D fill:#f9f,stroke:#333
    style I fill:#ff9,stroke:#333
```

### Detailed Process Flow

1. **Document Processing**:
   - A new document is processed by the ExtractorAgent which extracts entities and their claims
   - The extracted data is passed to the ReconcilerAgent

2. **Entity Lookup**:
   - The ReconcilerAgent queries Neo4j to find existing entities with the same name and type
   - It filters out entities from the same source document to avoid self-comparisons

3. **Conflict Detection**:
   - For each existing entity, the ReconcilerAgent builds a comparison candidate
   - It retrieves claims for both the new and existing entities
   - The claims are ranked by similarity to the entity name

4. **Conflict Resolution**:
   - The ReconcilerAgent constructs a detailed prompt for the LLM
   - The prompt includes:
     - Entity name and type
     - Descriptions from both sources
     - Relevant claims from both sources
   - The LLM analyzes the claims to determine if there's a contradiction

5. **Resolution Application**:
   - Based on the LLM's response, the ReconcilerAgent:
     - Creates conflict edges in Neo4j for unresolved conflicts
     - Updates entity confidence scores
     - Marks entities as superseded when newer information is more credible
     - Records resolution outcomes in the SourceReliability module

## Core Components

### ReconcilerAgent

The main class that implements the reconciliation logic.

```python
class ReconcilerAgent:
    def __init__(self):
        # Initialize backend (Groq or Mistral)
        # Initialize Neo4j client
        # Initialize SourceReliability tracker
    
    def reconcile(self, extracted: dict) -> dict:
        # Main reconciliation method
        # Processes extracted entities and claims
        # Returns reconciliation statistics
    
    def _detect_conflict(self, new_entity: dict, existing_entity: dict, new_doc: str) -> dict | None:
        # Builds comparison candidates for entity pairs
    
    def _resolve_conflict(self, conflict: dict, new_doc_claims: list[dict], new_doc_claim_vectors) -> dict:
        # Uses LLM to determine if there's a contradiction
        # Returns resolution decision with confidence score
    
    def _apply_resolution(self, new_entity: dict, existing_entity: dict, resolution: dict, filename: str):
        # Applies the resolution decision to the knowledge graph
    
    def _create_conflict_edge(self, new_entity: dict, existing_entity: dict, resolution: dict):
        # Creates a CONFLICTS_WITH relationship in Neo4j
    
    def _encode_claims(self, claims: list[dict]):
        # Encodes claims as vectors for efficient similarity search
```

### Key Methods

1. **reconcile()**: The main entry point that orchestrates the entire reconciliation process
2. **_detect_conflict()**: Identifies potential conflicts between entities
3. **_resolve_conflict()**: Uses LLM to determine the nature of conflicts and how to resolve them
4. **_apply_resolution()**: Updates the knowledge graph based on resolution decisions
5. **_create_conflict_edge()**: Records conflicts in the graph database
6. **_encode_claims()**: Pre-encodes claims to avoid redundant processing

## Integration with Other Modules

### Agent Framework Integration

The ReconcilerAgent is part of the agent framework and works closely with:

- **ExtractorAgent**: Provides the entities and claims to reconcile
- **CuratorAgent**: May provide curated entities for reconciliation
- **CodexAgent**: Processes documents that will be reconciled

### Memory Systems Integration

The ReconcilerAgent operates within the memory systems pipeline:

```mermaid
diagram TD
    A[Document] --> B[ExtractorAgent]
    B --> C[ReconcilerAgent]
    C --> D[Neo4j Database]
    C --> E[Qdrant Vector DB]
    D --> F[CuratorAgent]
    E --> G[CodexAgent]
    
    style C fill:#f9f,stroke:#333
```

### Data Processing Integration

The ReconcilerAgent depends on the data processing module for:

- **SourceReliability**: Tracks the credibility of different sources based on resolution outcomes
- **DomainClassifier**: Not directly used but part of the same data processing ecosystem

### Utilities Integration

The ReconcilerAgent uses:

- **QuotaTracker**: Manages API usage quotas to prevent overuse
- **Config**: Provides configuration settings for backend selection and model parameters

## Configuration

The ReconcilerAgent is configured through the system configuration:

```python
# In core/config.py
RECONCILER_BACKEND = "groq"  # or "mistral"
RECONCILER_MODEL = "llama3-70b-8192"  # Groq model
RECONCILER_MISTRAL_MODEL = "mistral-large-latest"  # Mistral model
```

## API Usage and Quotas

The ReconcilerAgent uses the Groq API (or Mistral API) to make LLM calls for conflict resolution. API usage is tracked and limited by the QuotaTracker to prevent excessive usage.

## Conflict Resolution Logic

The ReconcilerAgent uses a sophisticated prompt to guide the LLM in making resolution decisions:

```python
RECONCILER_PROMPT = """
You are a knowledge reconciliation engine with precise judgment.

Your job is to determine if two sources describing the same entity represent a REAL
CONTRADICTION or are simply COMPLEMENTARY information. This applies across ANY domain
KRONOS may encounter — people, organizations, products, specifications, financial figures,
legal terms, measurements, policies, historical events, or anything else. Do not assume
a particular domain; reason generally from the evidence given.

BASE YOUR JUDGMENT ON THE CLAIMS PROVIDED, NOT JUST THE DESCRIPTION WORDING.
Two entity descriptions can sound generic or similar ("researcher", "product", "policy")
while the underlying claims genuinely conflict on a specific attribute.
"""
```

### Resolution Outcomes

The LLM can return several types of resolution decisions:

1. **KEEP_DOC1**: The existing document is more credible for this attribute
2. **KEEP_DOC2**: The new document is more credible for this attribute
3. **KEEP_BOTH**: Both documents are valid (for complementary or duplicate information)
4. **FLAG_FOR_REVIEW**: The conflict requires human judgment
5. **MERGE**: The information from both documents should be combined

### Conflict Types

1. **CONTRADICTION**: Directly incompatible values for the same attribute
2. **UPDATE**: Newer document likely supersedes older (same attribute, plausible change over time)
3. **COMPLEMENTARY**: Different aspects of the same entity, both true simultaneously
4. **DUPLICATE**: Same information expressed differently

## Performance Considerations

1. **Efficient Claim Encoding**: Claims are encoded once upfront to avoid redundant processing
2. **Vector Similarity Search**: Uses cosine similarity for efficient claim ranking
3. **Batch Processing**: Processes multiple entities in a single reconciliation call
4. **Quota Management**: Tracks and limits API usage to prevent excessive costs

## Error Handling and Fallbacks

1. **Failed LLM Calls**: If the LLM call fails, the conflict is flagged for review
2. **Missing Claims**: If claims are unavailable, the reconciliation still proceeds with descriptions
3. **Unverified Conflicts**: Conflicts that can't be verified are flagged for later review

## Best Practices

1. **Regular Reconciliation**: Run reconciliation periodically to keep the knowledge graph consistent
2. **Source Credibility Tracking**: Monitor source reliability scores to identify problematic sources
3. **Human Review**: Use the FLAG_FOR_REVIEW resolution for complex conflicts that require human judgment
4. **Performance Monitoring**: Track reconciliation statistics to identify performance issues

## Monitoring and Metrics

The ReconcilerAgent provides detailed logging and returns statistics about the reconciliation process:

```json
{
  "filename": "document.pdf",
  "conflicts_found": 5,
  "conflicts_resolved": 3,
  "conflicts_flagged": 2,
  "unverified_count": 1,
  "details": [...]
}
```

## Troubleshooting

### Common Issues

1. **High Conflict Rate**: Indicates inconsistent sources or poor extraction quality
2. **Failed LLM Calls**: May indicate API issues or quota limits
3. **Performance Issues**: May indicate inefficient claim processing or database queries

### Debugging Tips

1. Check the detailed logs for specific conflict resolution decisions
2. Review the source reliability scores for problematic sources
3. Examine the Neo4j graph for conflict edges and their properties
4. Monitor API usage to ensure quotas aren't being exceeded

## Future Enhancements

1. **Machine Learning Integration**: Use ML models to improve conflict detection accuracy
2. **Automated Source Credibility**: Automatically adjust source credibility based on resolution outcomes
3. **Batch Reconciliation**: Process multiple documents in a single reconciliation call
4. **Conflict Resolution Patterns**: Identify and apply common resolution patterns automatically

## References

- [Agent Framework Documentation](agent_framework.md)
- [Data Processing Module Documentation](data_processing.md)
- [Utilities Module Documentation](utilities.md)
- [Neo4j Database Documentation](https://neo4j.com/docs/)
- [Qdrant Vector Database Documentation](https://qdrant.tech/documentation/)
- [Groq API Documentation](https://console.groq.com/docs)
- [Mistral AI Documentation](https://docs.mistral.ai/)