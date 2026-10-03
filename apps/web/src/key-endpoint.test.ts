import { describe, expect, it } from 'vitest'
import {
  bedrockModelName,
  blankDeclaration,
  effectiveShape,
  isBedrockHost,
  keyedProvider,
  shapesFor,
  suggestedShape,
  toDeclarations,
} from './key-endpoint'

describe('AWS Bedrock hosts', () => {
  it('recognises the Bedrock runtime hosts, as the API does', () => {
    expect(isBedrockHost('https://bedrock-runtime.us-east-1.amazonaws.com')).toBe(true)
    expect(isBedrockHost(' https://BEDROCK-RUNTIME.eu-west-1.amazonaws.com/ ')).toBe(true)
    expect(isBedrockHost('https://bedrock-mantle.us-west-2.api.aws')).toBe(true)
    expect(isBedrockHost('https://bedrock-runtime.us-east-1.amazonaws.com./')).toBe(true)
  })

  it('counts hosts MOSAIC refuses, so the API can say which host it needs', () => {
    expect(isBedrockHost('https://bedrock-runtime-fips.us-east-1.amazonaws.com')).toBe(true)
    expect(isBedrockHost('https://bedrock-runtime.cn-north-1.amazonaws.com.cn')).toBe(true)
    expect(isBedrockHost('https://bedrock.us-east-1.amazonaws.com')).toBe(true)
  })

  it('leaves every other host alone', () => {
    expect(isBedrockHost('https://fabrikam-foundry.services.ai.azure.com')).toBe(false)
    expect(isBedrockHost('https://s3.us-east-1.amazonaws.com')).toBe(false)
    expect(isBedrockHost('https://bedrock.example.com')).toBe(false)
    expect(isBedrockHost('https://bedrock-runtime.us-east-1.amazonaws.com.example.com')).toBe(false)
    expect(isBedrockHost('bedrock-runtime.us-east-1.amazonaws.com')).toBe(false)
    expect(isBedrockHost('')).toBe(false)
  })

  it("isn't taken for an Azure AI resource", () => {
    expect(keyedProvider('https://bedrock-runtime.us-east-1.amazonaws.com')).toBeNull()
  })
})

describe('Bedrock model names', () => {
  it('names the Claude model a Bedrock model ID serves the way Azure does', () => {
    expect(bedrockModelName('us.anthropic.claude-sonnet-4-5-20250929-v1:0')).toBe('claude-sonnet-4-5')
    expect(bedrockModelName('global.anthropic.claude-opus-4-5-20251101-v1:0')).toBe('claude-opus-4-5')
    expect(bedrockModelName('anthropic.claude-haiku-4-5-20251001-v1:0')).toBe('claude-haiku-4-5')
    expect(bedrockModelName('us-gov.anthropic.claude-sonnet-4-5-20250929-v1:0')).toBe('claude-sonnet-4-5')
    expect(bedrockModelName(' US.Anthropic.Claude-Opus-4-1-20250805-v1:0 ')).toBe('claude-opus-4-1')
    expect(bedrockModelName('anthropic.claude-3-5-sonnet-20240620-v1:0')).toBe('claude-3-5-sonnet')
  })

  it('suggests nothing for a model ID that serves no Claude model', () => {
    expect(bedrockModelName('amazon.nova-pro-v1:0')).toBe('')
    expect(bedrockModelName('meta.llama3-70b-instruct-v1:0')).toBe('')
    expect(bedrockModelName('us.anthropic.')).toBe('')
    expect(bedrockModelName('')).toBe('')
  })
})

describe('the API a declared model takes', () => {
  it('is always Anthropic Messages on AWS Bedrock', () => {
    expect(shapesFor('awsBedrock')).toEqual(['anthropicMessages'])
    expect(suggestedShape('', 'awsBedrock')).toBe('anthropicMessages')
    expect(suggestedShape('gpt-4o', 'awsBedrock')).toBe('anthropicMessages')
    expect(blankDeclaration('awsBedrock').apiShape).toBe('anthropicMessages')
  })

  it('declares Bedrock models with Anthropic Messages whatever a draft held', () => {
    const drafts = [
      {
        ...blankDeclaration('awsBedrock'),
        deploymentName: ' us.anthropic.claude-sonnet-4-5-20250929-v1:0 ',
        modelName: ' claude-sonnet-4-5 ',
      },
      { ...blankDeclaration('awsBedrock'), deploymentName: '', modelName: '  ' },
      {
        ...blankDeclaration(null),
        deploymentName: 'anthropic.claude-haiku-4-5-20251001-v1:0',
        modelName: 'claude-haiku-4-5',
        apiShape: 'foundryModels' as const,
        shapeChosen: true,
      },
    ]

    expect(effectiveShape(drafts[2], 'awsBedrock')).toBe('anthropicMessages')
    expect(toDeclarations(drafts, 'awsBedrock')).toEqual([
      {
        deploymentName: 'us.anthropic.claude-sonnet-4-5-20250929-v1:0',
        modelName: 'claude-sonnet-4-5',
        apiShape: 'anthropicMessages',
      },
      {
        deploymentName: 'anthropic.claude-haiku-4-5-20251001-v1:0',
        modelName: 'claude-haiku-4-5',
        apiShape: 'anthropicMessages',
      },
    ])
  })
})
